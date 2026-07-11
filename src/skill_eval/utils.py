"""Shared utility: call Claude CLI via subprocess."""

from __future__ import annotations

import os
import signal
import subprocess


def call_claude(
    prompt: str,
    model: str | None = None,
    timeout: int = 120,
    max_turns: int | None = None,
) -> str:
    cmd = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
    if max_turns is not None:
        cmd.extend(["--max-turns", str(max_turns)])
    if model:
        cmd.extend(["--model", model])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    # start_new_session 让外部 CLI(claude/opencode 等通用 backend)在独立进程组,
    # 超时方可 os.killpg 杀整组含其子进程树——这类 CLI 常是 wrapper + node worker,
    # 单 kill 主进程会让 worker 成孤儿、持有 stdout pipe 使 communicate 卡死
    # (实测 claude 曾卡 2588s 才报 300s timeout)。SIGKILL 不可忽略,确保彻底回收。
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        raise RuntimeError(f"claude CLI timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err[:500]}")
    return out
