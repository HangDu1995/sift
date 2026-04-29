"""SKILL.md parser — extracts frontmatter and body content."""

from __future__ import annotations

from pathlib import Path

import yaml

from skill_eval.errors import SkillParseError


def parse_skill_md(
    skill_path: Path,
) -> tuple[str, str, str, dict, str]:
    """Parse a SKILL.md file.

    Args:
        skill_path: Directory containing SKILL.md.

    Returns:
        (name, description, full_content, frontmatter_dict, body_text)

    Raises:
        SkillParseError: If the file is missing or malformed.
    """
    skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        raise SkillParseError(f"SKILL.md not found in {skill_path}")

    content = skill_file.read_text(encoding="utf-8")
    lines = content.split("\n")

    if not lines or lines[0].strip() != "---":
        raise SkillParseError("SKILL.md missing frontmatter (no opening ---)")

    end_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise SkillParseError("SKILL.md missing frontmatter (no closing ---)")

    frontmatter_text = "\n".join(lines[1:end_idx])

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillParseError(f"Invalid YAML in frontmatter: {exc}") from exc

    if not isinstance(frontmatter, dict):
        raise SkillParseError("Frontmatter must be a YAML dictionary")

    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()

    body_text = "\n".join(lines[end_idx + 1 :]).strip()

    return name, description, content, frontmatter, body_text
