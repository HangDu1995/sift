# tests/test_utils.py
"""call_claude 的 subprocess 行为:timeout 必须真生效(kill 整组,含孙进程)。
on_usage 回调(json 模式抓 token)+ TokenUsageAccumulator 并发累加。"""
import json
import os
import shlex
import stat
import time

import pytest

from skill_eval.utils import TokenUsageAccumulator, call_claude


def _make_fake_claude(tmp_path, body: str) -> None:
    """造一个假 `claude` 可执行,前置到 PATH,让 call_claude 调到它。"""
    p = tmp_path / "claude"
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _prepend_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


def test_call_claude_timeout_raises_within_window(monkeypatch, tmp_path):
    # 基本功能:超时抛 RuntimeError 且不长时间卡住。注意——WSL2 上假进程被
    # SIGKILL 即清理(~2s),无法用假进程复现真 claude 子进程树卡死(曾卡 2588s);
    # killpg 的真价值靠实测,这里只固化"超时抛错"的基本行为。
    _make_fake_claude(tmp_path, "trap '' TERM\nsleep 10")
    _prepend_path(monkeypatch, tmp_path)
    t0 = time.time()
    with pytest.raises(RuntimeError, match="timed out"):
        call_claude("x", timeout=2)
    elapsed = time.time() - t0
    assert elapsed < 5, f"超时后未及时返回:耗时 {elapsed:.1f}s"


def test_call_claude_runs_backend_in_new_process_group(monkeypatch, tmp_path):
    # 契约:外部 CLI 必须在独立进程组(start_new_session),超时方可 killpg 杀整组。
    # killpg 价值无法行为复现(见上),只能断言此契约在位,防重构丢失。
    from unittest.mock import MagicMock
    import skill_eval.utils as u
    proc = MagicMock()
    proc.communicate.return_value = ("out", "")
    proc.returncode = 0
    proc.pid = 99999
    mock_popen = MagicMock(return_value=proc)
    monkeypatch.setattr(u.subprocess, "Popen", mock_popen)
    call_claude("x", timeout=10)
    _, kwargs = mock_popen.call_args
    assert kwargs.get("start_new_session") is True


def test_call_claude_success_returns_stdout(monkeypatch, tmp_path):
    _make_fake_claude(tmp_path, "echo 'hello from claude'")
    _prepend_path(monkeypatch, tmp_path)
    assert call_claude("x", timeout=10).strip() == "hello from claude"


def test_call_claude_nonzero_exit_raises(monkeypatch, tmp_path):
    _make_fake_claude(tmp_path, "echo 'boom' >&2\nexit 1")
    _prepend_path(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="failed"):
        call_claude("x", timeout=10)


def test_call_claude_injects_claude_code_identity_when_absent(monkeypatch):
    # 智谱(open.bigmodel.cn)后端在模型过载时只放行带 claude-code 身份标识
    # (AI_AGENT + CLAUDE_CODE_ENTRYPOINT)的请求,裸 claude CLI 被直接 529 挡回。
    # 这俩变量由 VSCode 扩展/SDK 注入、用户终端默认没有 → 终端跑 audit 会系统性失败
    # (实测:同 token 同时刻,带这俩 OK、不带 529/超时)。故 call_claude 必须在子进程
    # env 缺这俩时补上。值版本无关(通用 'claude-code' 实测即放行)。
    from unittest.mock import MagicMock
    import skill_eval.utils as u
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    proc = MagicMock()
    proc.communicate.return_value = ("out", "")
    proc.returncode = 0
    proc.pid = 99999
    mock_popen = MagicMock(return_value=proc)
    monkeypatch.setattr(u.subprocess, "Popen", mock_popen)
    call_claude("x", timeout=10)
    _, kwargs = mock_popen.call_args
    env = kwargs["env"]
    assert env.get("AI_AGENT") == "claude-code"
    assert env.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode"


def test_call_claude_preserves_existing_identity(monkeypatch):
    # 在 claude-code 上下文内跑(VSCode 扩展/SDK 已注入真值)时,不覆盖、保留原值。
    from unittest.mock import MagicMock
    import skill_eval.utils as u
    monkeypatch.setenv("AI_AGENT", "real-agent-xyz")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "real-entrypoint")
    proc = MagicMock()
    proc.communicate.return_value = ("out", "")
    proc.returncode = 0
    proc.pid = 99999
    mock_popen = MagicMock(return_value=proc)
    monkeypatch.setattr(u.subprocess, "Popen", mock_popen)
    call_claude("x", timeout=10)
    _, kwargs = mock_popen.call_args
    env = kwargs["env"]
    assert env.get("AI_AGENT") == "real-agent-xyz"
    assert env.get("CLAUDE_CODE_ENTRYPOINT") == "real-entrypoint"


def test_call_claude_nonzero_exit_shows_stdout_when_stderr_empty(monkeypatch, tmp_path):
    # claude CLI 实测把输出/错误打 stdout、stderr 常空。报错信息必须能看到根因,
    # 不能是空的 "failed (exit 1): "(曾发生:后端不可用时 exit 1 + stderr 空,
    # 报错行冒号后空白,根因被吞)。优先 stderr、stderr 空则 fallback stdout。
    _make_fake_claude(tmp_path, "echo 'rate limit exceeded, retry later'\nexit 1")  # 错误打 stdout
    _prepend_path(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="rate limit exceeded") as exc:
        call_claude("x", timeout=10)
    assert "failed" in str(exc.value)


# ── on_usage 回调:json 模式抓 token ──────────────────────────────────────────
def _fake_claude_json(tmp_path, obj: dict) -> None:
    """造假 claude,stdout 吐给定 dict 的 json(单行)。"""
    _make_fake_claude(tmp_path, "echo " + shlex.quote(json.dumps(obj)))


def test_call_claude_with_usage_invokes_callback_and_returns_result(monkeypatch, tmp_path):
    # json 模式:解析单对象 → 返 result 文本;usage+cost 回调给 on_usage 供累加。
    obj = {
        "type": "result", "subtype": "success", "is_error": False, "result": "hello",
        "usage": {"input_tokens": 10, "output_tokens": 20,
                  "cache_read_input_tokens": 1, "cache_creation_input_tokens": 2},
        "total_cost_usd": 0.001,
    }
    _fake_claude_json(tmp_path, obj)
    _prepend_path(monkeypatch, tmp_path)
    acc = TokenUsageAccumulator()
    out = call_claude("x", timeout=10, on_usage=acc.add)
    assert out.strip() == "hello"
    snap = acc.snapshot()
    assert snap["input_tokens"] == 10
    assert snap["output_tokens"] == 20
    assert snap["cache_read_input_tokens"] == 1
    assert snap["cache_creation_input_tokens"] == 2
    assert snap["total_cost_usd"] == pytest.approx(0.001)
    assert snap["calls"] == 1


def test_call_claude_without_on_usage_uses_text_format(monkeypatch):
    # 默认 on_usage=None → text 模式(历史行为,字节不变)。
    from unittest.mock import MagicMock
    import skill_eval.utils as u
    proc = MagicMock(); proc.communicate.return_value = ("out", ""); proc.returncode = 0; proc.pid = 99999
    mock_popen = MagicMock(return_value=proc)
    monkeypatch.setattr(u.subprocess, "Popen", mock_popen)
    call_claude("x", timeout=10)
    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("--output-format") + 1] == "text"


def test_call_claude_with_on_usage_uses_json_format(monkeypatch):
    # 给 on_usage → json 模式。
    from unittest.mock import MagicMock
    import skill_eval.utils as u
    proc = MagicMock(); proc.communicate.return_value = ('{"result":"x","usage":{}}', ""); proc.returncode = 0; proc.pid = 99999
    mock_popen = MagicMock(return_value=proc)
    monkeypatch.setattr(u.subprocess, "Popen", mock_popen)
    call_claude("x", timeout=10, on_usage=lambda d: None)
    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_call_claude_json_parse_failure_returns_raw_no_callback(monkeypatch, tmp_path):
    # 兜底:输出不是合法 json → 退回 raw stdout、不触发 on_usage(不砸拿结果)。
    _make_fake_claude(tmp_path, "echo 'this is not json at all'")
    _prepend_path(monkeypatch, tmp_path)
    acc = TokenUsageAccumulator()
    out = call_claude("x", timeout=10, on_usage=acc.add)
    assert "this is not json" in out
    assert acc.snapshot().get("calls", 0) == 0  # 不回调(snapshot 空)


def test_call_claude_usage_missing_skips_callback(monkeypatch, tmp_path):
    # 对象无 usage 字段 → 返 result、不回调(token 不可信就别报)。
    _fake_claude_json(tmp_path, {"type": "result", "result": "hi"})
    _prepend_path(monkeypatch, tmp_path)
    acc = TokenUsageAccumulator()
    out = call_claude("x", timeout=10, on_usage=acc.add)
    assert out == "hi"
    assert acc.snapshot().get("calls", 0) == 0  # 无 usage 不回调


# ── TokenUsageAccumulator:累加 + 并发安全 ────────────────────────────────────
def test_accumulator_adds_and_sums():
    acc = TokenUsageAccumulator()
    acc.add({"input_tokens": 10, "output_tokens": 20, "total_cost_usd": 0.01})
    acc.add({"input_tokens": 5, "output_tokens": 1, "total_cost_usd": 0.02})
    snap = acc.snapshot()
    assert snap["input_tokens"] == 15
    assert snap["output_tokens"] == 21
    assert snap["total_cost_usd"] == pytest.approx(0.03)
    assert snap["calls"] == 2


def test_accumulator_missing_keys_default_zero():
    acc = TokenUsageAccumulator()
    acc.add({})  # 缺键不 KeyError
    snap = acc.snapshot()
    assert snap["input_tokens"] == 0
    assert snap["calls"] == 1


def test_accumulator_concurrent_adds_thread_safe():
    # audit 判官在线程池并发触发 on_usage;锁必须防丢失更新。
    import threading
    acc = TokenUsageAccumulator()

    def worker():
        for _ in range(50):
            acc.add({"input_tokens": 1})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert acc.snapshot()["input_tokens"] == 400  # 8 × 50,无丢失更新


def test_accumulator_snapshot_is_copy():
    acc = TokenUsageAccumulator()
    acc.add({"input_tokens": 5})
    snap = acc.snapshot()
    snap["input_tokens"] = 999  # 改副本
    assert acc.snapshot()["input_tokens"] == 5  # 内部不受影响


def test_accumulator_empty_when_no_calls():
    # 没累加过(calls==0)→ snapshot 返空 dict(无 LLM 调用的 audit,report 保持干净)。
    acc = TokenUsageAccumulator()
    assert acc.snapshot() == {}
