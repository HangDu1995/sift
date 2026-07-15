# tests/test_utils.py
"""call_claude 的 subprocess 行为:timeout 必须真生效(kill 整组,含孙进程)。"""
import os
import stat
import time

import pytest

from skill_eval.utils import call_claude


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
