"""Shared utility: call Claude CLI via subprocess."""

from __future__ import annotations

import os
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
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {timeout}s")
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
    return result.stdout
