"""OpenCodeRunner 单测:mock subprocess.Popen + run_opencode,不真跑 opencode。"""

from __future__ import annotations

import json
import signal
from contextlib import contextmanager

import pytest

from sift.execution import opencode_runner as ocr
from sift.execution.options import ExecutionOptions


@contextmanager
def _yield_ctx(p):
    yield p


# 模拟 opencode --format json 流(成功跑完,带 step_finish tokens)
_STREAM_LINES = [
    ('{"type":"step_start","part":{"type":"step-start"}}\n'),
    ('{"type":"text","part":{"type":"text","text":"PONG"}}\n'),
    ('{"type":"tool_use","part":{"type":"tool_use","tool":"read","state":{"input":{}}}}\n'),
    (
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop","tokens"'
        ':{"total":7300,"input":4993,"output":3,"cache":{"read":2304,"write":0}}}}\n'
    ),
]


class _FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeStream:
    def __init__(self, lines_bytes):
        self._lines = list(lines_bytes)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, stdout_lines, returncode=0, stderr=b""):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream([l.encode() for l in stdout_lines])
        self.stderr = _FakeStream([stderr] if stderr else [])
        self.returncode = returncode
        self.pid = 12345

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def patched_spawn(monkeypatch, tmp_path):
    """patch Popen/which/run_opencode/isolated_xdg_env_empty;返回捕获 dict。"""
    cap = {"popen_args": None, "popen_kwargs": None, "run_calls": [], "which": "/fake/opencode"}

    def fake_popen(args, **kwargs):
        cap["popen_args"] = list(args)
        cap["popen_kwargs"] = kwargs
        return cap.get("_proc") or _FakeProc(_STREAM_LINES)

    def fake_which(name):
        return cap["which"]

    def fake_run_opencode(xdg, args, timeout=120):
        cap["run_calls"].append(list(args))
        if args[:2] == ["session", "list"]:
            return json.dumps([{"id": "ses_abc", "title": "t"}])
        if args[0] == "export":
            return (
                '{"info": {"tokens": {"input": 4993}}, "messages": [{"info": {"role": '
                '"user"}, "parts": [{"type": "text", "text": "hi"}]}, {"info": {"role": '
                '"assistant"}, "parts": [{"type": "text", "text": "PONG"}]}]}'
            )
        return ""

    monkeypatch.setattr(ocr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ocr.shutil, "which", fake_which)
    monkeypatch.setattr(ocr, "run_opencode", fake_run_opencode)
    monkeypatch.setattr(ocr, "isolated_xdg_env_empty", lambda: _yield_ctx(tmp_path))
    return cap


def test_run_builds_cmd_and_exports_raw(patched_spawn, tmp_path):
    out = ocr.OpenCodeRunner().run(
        prompt="do it",
        skill_path=None,
        options=ExecutionOptions(model="zai-anthropic/glm-5.2", cwd=tmp_path, timeout=30),
    )
    a = patched_spawn["popen_args"]
    assert a[0:4] == ["opencode", "run", "--format", "json"]
    assert "--dangerously-skip-permissions" in a
    assert "--dir" in a and str(tmp_path) in a
    assert "-m" in a and "zai-anthropic/glm-5.2" in a  # 含 / → 传
    assert a[-1] == "do it"  # prompt 末位
    export_call = [c for c in patched_spawn["run_calls"] if c[0] == "export"][0]
    assert export_call == ["export", "ses_abc"]
    assert "--sanitize" not in export_call  # 原文,不脱敏
    assert json.loads(out.stdout)["messages"][1]["parts"][0]["text"] == "PONG"
    assert out.total_tokens == 7300
    assert out.stop_reason == "stop"
    assert out.num_turns == 1  # _STREAM_LINES 恰好 1 个 step_finish


def test_run_bare_model_skipped(patched_spawn, tmp_path):
    """裸名 model(无 /)→ 不传 -m,走 opencode config 默认。"""
    ocr.OpenCodeRunner().run(
        prompt="t", skill_path=None, options=ExecutionOptions(model="glm-5.2[1m]", cwd=tmp_path, timeout=30)
    )
    assert "-m" not in patched_spawn["popen_args"]


def test_run_env_has_api_key_alias_and_xdg(patched_spawn, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ocr.OpenCodeRunner().run(prompt="t", skill_path=None, options=ExecutionOptions(cwd=tmp_path, timeout=30))
    env = patched_spawn["popen_kwargs"]["env"]
    assert env["ANTHROPIC_API_KEY"] == "tok-xyz"  # alias from AUTH_TOKEN
    assert "XDG_DATA_HOME" in env


def test_run_nonzero_exit_raises(patched_spawn, tmp_path):
    patched_spawn["_proc"] = _FakeProc(_STREAM_LINES, returncode=2, stderr=b"auth fail")
    with pytest.raises(ocr.ExecutionError, match="auth fail"):
        ocr.OpenCodeRunner().run(prompt="t", skill_path=None, options=ExecutionOptions(cwd=tmp_path, timeout=30))
    # returncode != 0 必须在 export 前抛 → 不该有任何 export 调用(锁顺序,防假绿)
    assert not any(c[0] == "export" for c in patched_spawn["run_calls"])


def test_run_opencode_missing_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    with pytest.raises(ocr.ExecutionError, match="opencode CLI 未找到"):
        ocr.OpenCodeRunner().run(prompt="t", skill_path=None, options=ExecutionOptions(cwd=tmp_path, timeout=30))


def test_run_closes_stdin(patched_spawn, tmp_path):
    proc = _FakeProc(_STREAM_LINES)
    patched_spawn["_proc"] = proc
    ocr.OpenCodeRunner().run(prompt="t", skill_path=None, options=ExecutionOptions(cwd=tmp_path, timeout=30))
    assert proc.stdin.closed is True


def test_run_timeout_kills_process_group_and_raises(monkeypatch, tmp_path):
    """超时 → killpg(SIGKILL) 整个进程组 + ExecutionError。最绕的路径,要有覆盖。"""
    import subprocess as _sp

    killed_gids = []
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/fake/opencode")
    monkeypatch.setattr(ocr, "run_opencode", lambda *a, **k: json.dumps([{"id": "ses_x"}]))
    monkeypatch.setattr(ocr, "isolated_xdg_env_empty", lambda: _yield_ctx(tmp_path))
    monkeypatch.setattr(ocr.os, "killpg", lambda gid, sig: killed_gids.append((gid, sig)))
    # start_new_session=True → 子进程是自己的组长,os.getpgid(child_pid) == child_pid;
    # 真实 getpgid 对假 pid 会抛 ProcessLookupError 被 runner 吞掉走 kill 分支,测不到 killpg,
    # 故 patch 成 echo 以忠实模拟进程组不变量。
    monkeypatch.setattr(ocr.os, "getpgid", lambda pid: pid)

    class _TimeoutProc(_FakeProc):
        def __init__(self):
            super().__init__(_STREAM_LINES)
            self._wait_calls = 0

        def wait(self, timeout=None):
            self._wait_calls += 1
            if self._wait_calls == 1:
                raise _sp.TimeoutExpired(cmd="opencode", timeout=1)
            return 0

    proc = _TimeoutProc()
    monkeypatch.setattr(ocr.subprocess, "Popen", lambda args, **kw: proc)
    with pytest.raises(ocr.ExecutionError, match="超时"):
        ocr.OpenCodeRunner().run(prompt="t", skill_path=None, options=ExecutionOptions(cwd=tmp_path, timeout=1))
    # start_new_session=True → 子进程是组长,pgid == pid;killpg 收到的 gid 应是 proc.pid
    assert killed_gids and killed_gids[0][0] == proc.pid
    assert killed_gids[0][1] == signal.SIGKILL
