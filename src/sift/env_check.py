"""Check environment prerequisites before running evals."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

_TOOL_PATTERNS: dict[str, str] = {
    "gh": r"\bgh\b",
    "github": r"github|gh\b",
    "kubectl": r"kubectl",
    "docker": r"\bdocker\b",
    "aws": r"\baws\b",
    "npm": r"\bnpm\b|npx\b",
    "gradle": r"gradle",
    "maven": r"\bmvn\b|maven",
    "java": r"\bjava\b",
    "node": r"\bnode\b",
    "python": r"\bpython3?\b",  # matches both python and python3
}

# Tools that have multiple possible binary names
_ALIASES: dict[str, list[str]] = {
    "python": ["python3", "python"],
    "node": ["node", "nodejs"],
}


def _find_python() -> list[str]:
    """Find all available python binaries on PATH."""
    import os

    candidates = []
    for path in os.environ.get("PATH", "").split(os.pathsep):
        _collect_python_candidates(path, candidates)
    return sorted(set(candidates), reverse=True)


def _check_available(tool: str) -> bool:
    """Check if a tool is available, trying aliases if necessary."""
    if shutil.which(tool):
        return True
    for alias in _ALIASES.get(tool, []):
        if not (shutil.which(alias)):
            continue
        return True
    if tool == "python":
        for _ in _find_python():
            return True
    return False


def _check_available(tool: str) -> bool:
    """Check if a tool is available, trying aliases if necessary."""
    if shutil.which(tool):
        return True
    for alias in _ALIASES.get(tool, []):
        if not (shutil.which(alias)):
            continue
        return True
    return False


def check_skill_env(skill_path: Path) -> dict[str, bool]:
    """Check which tools referenced in SKILL.md are available locally.

    Returns {tool_name: available_bool}. Only includes tools that were
    mentioned in SKILL.md.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return {}

    content = skill_md.read_text(encoding="utf-8", errors="replace")

    return _check_tools_in_text(content)


def check_eval_env(eval_prompts: list[str]) -> dict[str, bool]:
    """Check tools referenced in eval prompts."""
    content = " ".join(eval_prompts)
    return _check_tools_in_text(content)


def _collect_python_candidates(path, candidates):
    import os

    try:
        for entry in os.listdir(path):
            if not (
                entry in ("python", "python3")
                or (entry.startswith("python3.") and all(c.isdigit() or c == "." for c in entry[7:]))
            ):
                continue
            full = os.path.join(path, entry)
            if os.access(full, os.X_OK) and entry not in candidates:
                candidates.append(entry)
    except OSError:
        logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)
        return


def _check_tools_in_text(content):
    results: dict[str, bool] = {}
    seen: set[str] = set()

    for tool, pattern in _TOOL_PATTERNS.items():
        if tool in seen:
            continue
        if not (re.search(pattern, content, re.IGNORECASE)):
            continue
        seen.add(tool)
        results[tool] = _check_available(tool)

    return results
