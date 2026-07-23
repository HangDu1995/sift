"""OpenCodeRunner — 路径1 真任务模式的 opencode 执行器(与 ClaudeRunner 对偶)。

同签名 ``run(prompt, skill_path, model, timeout, cwd, max_turns, on_progress) -> RunOutput``。
spawn ``opencode run <prompt> --format json --dangerously-skip-permissions --dir <cwd>
[-m provider/model]``,在隔离 XDG_DATA_HOME 下跑(会话落一次性 db、不污染真库),
跑完 export 刚建的 session(**不带 --sanitize**,audit 要原文)→ ``RunOutput.stdout``。

实战模式抄 opsbot src/opsbot/driver/opencode.py(spike-derived):
- ``_ensure_api_key``:opencode @ai-sdk/anthropic 只读 ANTHROPIC_API_KEY,本机 GLM
  Coding Plan 给的是 ANTHROPIC_AUTH_TOKEN → alias 过来。
- model 含 ``/`` 才传 ``-m``(裸名 opencode API 不认 → 跳过走 config 默认)。
- stdin 关 EOF 避免 non-TTY 挂起;``--dangerously-skip-permissions`` headless 必带
  (opencode 无 --permission-mode);进程组 + killpg 超时清理。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from skill_eval.errors import ExecutionError
from skill_eval.evals.opencode_db import _isolated_xdg_env_empty, _run_opencode
from skill_eval.execution.cli_runner import build_live_prompt
from skill_eval.models import RunOutput
from skill_eval.utils import claude_cli_env


def _ensure_api_key(env: dict[str, str]) -> dict[str, str]:
    """opencode @ai-sdk/anthropic 只读 ANTHROPIC_API_KEY;本机 GLM Coding Plan 给的是
    ANTHROPIC_AUTH_TOKEN → 缺 API_KEY 时 alias 过来(抄 opsbot driver/opencode.py:221)。"""
    if not env.get("ANTHROPIC_API_KEY") and env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_API_KEY"] = env["ANTHROPIC_AUTH_TOKEN"]
    return env


def _progress_from_opencode_line(line: str) -> str | None:
    """opencode --format json 一行 → 进度串(供 on_progress 实时吐),无足轻重返 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    typ = ev.get("type")
    part = ev.get("part") or {}
    if typ == "text":
        t = part.get("text", "")
        return f"💬 {t[:80]}" if t else None
    if typ == "tool_use":
        return f"🔧 {part.get('tool', '?')}"
    if typ == "step_finish":
        tok = part.get("tokens") or {}
        return f"✓ {part.get('reason', '')}, {tok.get('total', 0)} tok"
    return None


def _parse_opencode_stream(stdout: str) -> tuple[int, dict[str, int], str, str, int]:
    """opencode --format json 流 → (total_tokens, tool_calls, result_text, stop_reason, num_turns)。best-effort。

    transcript 走 export(权威);这里只抽 run_debug.json 用的 operational metrics。
    事件形状:text / tool_use(call+result 合并)/ step_finish(tokens+reason)。
    num_turns = step_finish 事件计数(每个 ≈ 一轮 agent turn)。
    """
    total_tokens = 0
    tool_calls: dict[str, int] = {}
    result_text = ""
    stop_reason = ""
    num_turns = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = ev.get("type")
        part = ev.get("part") or {}
        if typ == "text":
            t = part.get("text", "")
            if t:
                result_text = t  # 末条 assistant text 胜出
        elif typ == "tool_use":
            name = part.get("tool", "")
            if name:
                tool_calls[name] = tool_calls.get(name, 0) + 1
        elif typ == "step_finish":
            num_turns += 1
            tok = part.get("tokens") or {}
            tot = tok.get("total") or ((tok.get("input") or 0) + (tok.get("output") or 0))
            if tot:
                total_tokens = tot
            r = part.get("reason")
            if r:
                stop_reason = r
    return total_tokens, tool_calls, result_text, stop_reason, num_turns


class OpenCodeRunner:
    def run(
        self,
        prompt: str,
        skill_path: Path | None = None,
        model: str | None = None,
        timeout: int = 120,
        cwd: Path | None = None,
        max_turns: int = 10,
        on_progress: Callable[[str], None] | None = None,
    ) -> RunOutput:
        if shutil.which("opencode") is None:
            raise ExecutionError(
                "opencode CLI 未找到;--executor opencode 需先装 opencode 并确保在 PATH"
                "(https://opencode.ai)"
            )
        injected = build_live_prompt(prompt, skill_path)
        workspace = cwd or Path.cwd()

        args = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions",
                "--dir", str(workspace)]
        if model and "/" in model:
            args.extend(["-m", model])
        args.append(injected)

        start = time.time()
        with _isolated_xdg_env_empty() as xdg:
            env = _ensure_api_key({**claude_cli_env(), "XDG_DATA_HOME": str(xdg)})
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=str(workspace),
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise ExecutionError(f"opencode CLI 启动失败: {exc}") from exc

            # stdin 关 EOF:避免 opencode non-TTY stdin 挂起(opsbot spike finding)
            assert proc.stdin is not None
            proc.stdin.close()

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            def _stdout_reader() -> None:
                assert proc.stdout is not None
                for raw in iter(proc.stdout.readline, b""):
                    text = raw.decode("utf-8", errors="replace")
                    stdout_chunks.append(text)
                    if on_progress:
                        msg = _progress_from_opencode_line(text)
                        if msg:
                            on_progress(msg)

            def _stderr_reader() -> None:
                assert proc.stderr is not None
                for raw in iter(proc.stderr.readline, b""):
                    stderr_chunks.append(raw.decode("utf-8", errors="replace"))

            t_out = threading.Thread(target=_stdout_reader, daemon=True)
            t_err = threading.Thread(target=_stderr_reader, daemon=True)
            t_out.start()
            t_err.start()
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    proc.kill()
                proc.wait()
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            if timed_out:
                raise ExecutionError(f"opencode run 超时({timeout}s)")

            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            if proc.returncode != 0:
                raise ExecutionError(
                    f"opencode run 失败(exit {proc.returncode}): {stderr[:500]}"
                )

            # export 刚跑的 session;不带 --sanitize(audit 要原文)
            try:
                sessions = json.loads(_run_opencode(xdg, ["session", "list", "--format", "json"]).strip() or "[]")
            except json.JSONDecodeError:
                sessions = []
            if not sessions:
                raise ExecutionError(
                    f"opencode run 未持久化 session(无法 export;stderr: {stderr[:300]})"
                )
            # 隔离 XDG db 是 fresh 的,只含本次 opencode run 刚建的那条 session → sessions[0] 即它;
            # 若 opencode 将来会额外建辅助 session,改按 timestamp 过滤而非盲取 [0]。
            sid = sessions[0].get("id", "")
            transcript_json = _run_opencode(xdg, ["export", sid])  # 原文,不 sanitize

            total_tokens, tool_calls, result_text, stop_reason, num_turns = _parse_opencode_stream(stdout)
            duration_ms = int((time.time() - start) * 1000)
            return RunOutput(
                exit_code=proc.returncode,
                stdout=transcript_json,
                duration_ms=duration_ms,
                total_tokens=total_tokens,
                tool_calls=tool_calls,
                result_text=result_text,
                stop_reason=stop_reason,
                num_turns=num_turns,
            )
