"""L0 eval coverage scanner — scan skills, validate evals.json, compute assertion coverage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from skill_eval.models import AssertionType

PROGRAMMATIC_TYPES = {t.value for t in AssertionType} - {"CUSTOM_SCRIPT"}

VALID_ASSERTION_TYPES = {t.value for t in AssertionType} | {"llm_judge", "behavior_list"}


def scan_evals(
    root_dir: Path | str,
    filters: dict | None = None,
    config: Any | None = None,
) -> dict:
    root = Path(root_dir).resolve()
    filters = filters or {}

    prefix = ""
    skills_dir_name = "skills/"
    if config is not None:
        prefix = getattr(config, "collection_prefix", "") or ""
        skills_dir_name = getattr(config, "skills_dir", "skills/")
    prefix = prefix.rstrip("*") if prefix else ""

    skills: list[dict] = []
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        entries = []

    for entry in entries:
        if prefix and not entry.name.startswith(prefix):
            continue
        if not entry.is_dir():
            continue
        skills_dir = entry / skills_dir_name.rstrip("/")
        if not skills_dir.is_dir():
            continue
        for skill_entry in sorted(skills_dir.iterdir()):
            skill_dir = skills_dir / skill_entry
            if not skill_dir.is_dir():
                continue
            skill_data = _parse_skill(skill_dir)
            if _matches_filters(skill_data, filters):
                skills.append(skill_data)

    with_evals = [s for s in skills if s["has_evals"]]
    without_evals = [s for s in skills if not s["has_evals"]]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_skills": len(skills),
        "with_evals": len(with_evals),
        "without_evals": len(without_evals),
        "to_generate": [s["name"] for s in without_evals],
        "skills": skills,
    }


def validate_evals_json(evals_path: Path | str) -> list[dict]:
    path = Path(evals_path)
    issues: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [{"severity": "ERROR", "message": f"Cannot read evals.json: {exc}"}]

    if isinstance(data, list):
        for ev in data:
            _validate_single_eval(ev, issues, "evals")
        return issues

    if isinstance(data, dict):
        if "skill_name" not in data:
            issues.append({"severity": "ERROR", "message": "Missing required field: skill_name"})
        for section in ("unit_evals", "integration_evals", "evals"):
            section_evals = data.get(section, [])
            if isinstance(section_evals, list):
                for ev in section_evals:
                    _validate_single_eval(ev, issues, section)
    return issues


def check_assertion_coverage(evals_data: dict) -> float:
    all_evals: list[dict] = []
    if isinstance(evals_data, list):
        all_evals = list(evals_data)
    elif isinstance(evals_data, dict):
        for section in ("unit_evals", "integration_evals", "evals"):
            section_evals = evals_data.get(section, [])
            if isinstance(section_evals, list):
                all_evals.extend(section_evals)

    if not all_evals:
        return 0.0

    count = 0
    for ev in all_evals:
        assertions = ev.get("assertions", [])
        if any(a.get("type") in PROGRAMMATIC_TYPES for a in assertions):
            count += 1
    return count / len(all_evals)


def _parse_skill(skill_dir: Path) -> dict:
    skill_md = skill_dir / "SKILL.md"
    frontmatter = _extract_frontmatter(skill_md)

    evals_path = ""
    for candidate in ("evals/evals.json", "evals.json"):
        p = skill_dir / candidate
        if p.exists():
            evals_path = str(p)
            break

    has_evals = bool(evals_path)
    eval_count = 0
    eval_issues: list[dict] = []

    if has_evals:
        try:
            with open(evals_path, encoding="utf-8") as f:
                evals_data = json.load(f)
            if isinstance(evals_data, list):
                eval_count = len(evals_data)
            elif isinstance(evals_data, dict):
                for section in ("unit_evals", "integration_evals", "evals"):
                    section_evals = evals_data.get(section, [])
                    if isinstance(section_evals, list):
                        eval_count += len(section_evals)
            eval_issues = validate_evals_json(evals_path)
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "name": frontmatter.get("name", skill_dir.name),
        "has_evals": has_evals,
        "eval_count": eval_count,
        "type": frontmatter.get("type"),
        "domain": frontmatter.get("domain"),
        "tags": frontmatter.get("tags", []),
        "modes": frontmatter.get("modes", []),
        "skill_dir": str(skill_dir),
        "issues": eval_issues,
    }


def _extract_frontmatter(md_path: Path) -> dict:
    if not md_path.exists():
        return {}
    try:
        content = md_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    rest = content[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    try:
        parsed = yaml.safe_load(rest[:end])
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


def _matches_filters(skill_data: dict, filters: dict) -> bool:
    if "type" in filters and skill_data.get("type") != filters["type"]:
        return False
    if "domain" in filters and skill_data.get("domain") != filters["domain"]:
        return False
    if "tags" in filters:
        skill_tags = set(skill_data.get("tags", []))
        if not set(filters["tags"]).intersection(skill_tags):
            return False
    if "names" in filters and skill_data["name"] not in filters["names"]:
        return False
    return True


def _validate_single_eval(ev: dict, issues: list[dict], section: str) -> None:
    eval_id = ev.get("id", "?")
    if "prompt" not in ev:
        issues.append({"severity": "ERROR", "message": f"{section} eval id={eval_id}: missing prompt"})
    elif not ev["prompt"] or not ev["prompt"].strip():
        issues.append({"severity": "ERROR", "message": f"{section} eval id={eval_id}: empty prompt"})

    assertions = ev.get("assertions")
    if assertions is None or (isinstance(assertions, list) and len(assertions) == 0):
        issues.append({"severity": "WARN", "message": f"{section} eval id={eval_id}: no assertions"})
    elif isinstance(assertions, list):
        for a in assertions:
            atype = a.get("type", "")
            if atype not in VALID_ASSERTION_TYPES:
                issues.append({"severity": "ERROR", "message": f"{section} eval id={eval_id}: unknown assertion type '{atype}'"})
