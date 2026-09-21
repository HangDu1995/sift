"""Claude CLI version checking and compatibility.

sift depends on Claude Code CLI (``claude``) for running evaluations,
A/B tests, LLM grading, and skill-trigger optimization.  It communicates
with Claude via the Agent SDK / headless mode (``claude -p``) and relies on
specific CLI flags.

Supported version range
~~~~~~~~~~~~~~~~~~~~~~~
- Minimum: **2.1.0** (2026-01-07) — first stable 2.1.x release with all
  required flags (``--disable-slash-commands`` landed in 2.0.60).
- Soft maximum: **2.2.0** — versions >= 2.2.0 *may* work but are not yet
  tested; a warning is printed instead of hard-blocking.

Flags consumed by sift
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``--output-format {text,json,stream-json}`` — structured output parsing
- ``--verbose`` — full turn-by-turn output
- ``--dangerously-skip-permissions`` — unattended CI/scripted execution
- ``--max-turns`` — limit agentic turns per query
- ``--disable-slash-commands`` — isolate ``without_skill`` baseline from
  auto-discovered skills
- ``--include-partial-messages`` — streaming token deltas
- ``--model`` — model selection per run

References
~~~~~~~~~~
- CLI reference: https://docs.anthropic.com/en/docs/claude-code/cli-usage
- Headless / Agent SDK: https://docs.anthropic.com/en/docs/claude-code/headless
- Changelog: https://docs.anthropic.com/en/docs/claude-code/changelog
"""

from __future__ import annotations

import re
import subprocess

from sift.executables import resolve_executable

CLAUDE_MIN_VERSION = "2.1.0"
CLAUDE_MAX_VERSION = "2.2.0"


def get_claude_version() -> str | None:
    try:
        proc = subprocess.run(
            [resolve_executable("claude"), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        output = proc.stdout.strip()
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        return match.group(1) if match else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def check_claude_available() -> tuple[bool, str]:
    version = get_claude_version()
    if version is None:
        return False, (
            "claude CLI not found. Install Claude Code: https://docs.anthropic.com/en/docs/claude-code/overview"
        )
    return True, version


def check_claude_version() -> tuple[bool, str]:
    version = get_claude_version()
    if version is None:
        return False, (
            "claude CLI not found. Install Claude Code >= "
            f"{CLAUDE_MIN_VERSION}: https://docs.anthropic.com/en/docs/claude-code/overview"
        )

    parts = [int(x) for x in version.split(".")]
    min_parts = [int(x) for x in CLAUDE_MIN_VERSION.split(".")]
    max_parts = [int(x) for x in CLAUDE_MAX_VERSION.split(".")]

    if parts < min_parts:
        return False, (
            f"claude CLI version {version} is too old. sift requires >= {CLAUDE_MIN_VERSION}. Run: claude update"
        )
    if parts >= max_parts:
        return True, (
            f"claude CLI version {version} (>= {CLAUDE_MAX_VERSION}). "
            f"sift is tested with {CLAUDE_MIN_VERSION}..{CLAUDE_MAX_VERSION}*. "
            f"Please report any issues."
        )
    return True, f"claude CLI version {version} (compatible)"


def requires_claude():
    available, msg = check_claude_available()
    if not available:
        from sift.errors import ClaudeRunnerError

        raise ClaudeRunnerError(msg)
    return msg
