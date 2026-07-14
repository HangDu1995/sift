"""SKILL.md parser — extracts frontmatter and body content."""

from __future__ import annotations

from pathlib import Path

import yaml

from skill_eval.errors import SkillParseError


def resolve_skill_dir(skill_path: Path) -> Path:
    """规范化 skill 路径:传 SKILL.md 文件 → 取父目录;传目录(或不存在的路径)→ 原样。

    用户常误传 SKILL.md 文件而非其所在目录,统一兜底,让所有
    "用 skill_path 找 SKILL.md / references / scripts / evals"的下游拿到目录。
    """
    return skill_path.parent if skill_path.is_file() else skill_path


def parse_skill_md(
    skill_path: Path,
) -> tuple[str, str, str, dict, str]:
    """Parse a SKILL.md file.

    Args:
        skill_path: Directory containing SKILL.md, or the SKILL.md file itself.

    Returns:
        (name, description, full_content, frontmatter_dict, body_text)

    Raises:
        SkillParseError: If the file is missing or malformed.
    """
    skill_path = resolve_skill_dir(skill_path)
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
