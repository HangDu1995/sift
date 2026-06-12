"""Check environment prerequisites before running evals."""

from __future__ import annotations

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
    "python": r"\bpython3?\b",
}


def check_skill_env(skill_path: Path) -> dict[str, bool]:
    """Check which tools referenced in SKILL.md are available locally.

    Returns {tool_name: available_bool}. Only includes tools that were
    mentioned in SKILL.md.
    """
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return {}

    content = skill_md.read_text(encoding="utf-8", errors="replace")

    results: dict[str, bool] = {}
    seen: set[str] = set()

    for tool, pattern in _TOOL_PATTERNS.items():
        if tool in seen:
            continue
        if re.search(pattern, content, re.IGNORECASE):
            seen.add(tool)
            results[tool] = shutil.which(tool) is not None

    return results


def check_eval_env(eval_prompts: list[str]) -> dict[str, bool]:
    """Check tools referenced in eval prompts."""
    content = " ".join(eval_prompts)
    results: dict[str, bool] = {}
    seen: set[str] = set()

    for tool, pattern in _TOOL_PATTERNS.items():
        if tool in seen:
            continue
        if re.search(pattern, content, re.IGNORECASE):
            seen.add(tool)
            results[tool] = shutil.which(tool) is not None

    return results
