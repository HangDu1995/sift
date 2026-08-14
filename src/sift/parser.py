"""SKILL.md parser — extracts frontmatter and body content."""

from __future__ import annotations

from pathlib import Path

import yaml

from sift.errors import SkillParseError


def resolve_skill_dir(skill_path: Path) -> Path:
    """规范化 skill 路径:**名为 SKILL.md 的文件** → 取父目录;其余 → 原样。

    只兜底"用户误传 SKILL.md 文件而非目录"这一种(让找 references/scripts/evals 的下游拿到目录)。
    **非 SKILL.md 的 .md 文件是 agent/workflow 声明**(audit --kind agent/root 故意直传),
    不动它——身份靠 frontmatter `name`,不靠文件名。
    """
    if skill_path.is_file() and skill_path.name == "SKILL.md":
        return skill_path.parent
    return skill_path


def parse_skill_md(
    skill_path: Path,
) -> tuple[str, str, str, dict, str]:
    """Parse a declaration file:SKILL.md,或任意 agent/workflow 声明 .md。

    Args:
        skill_path: 目录(找其下 SKILL.md)、SKILL.md 文件、或任意 .md 声明文件
                    (agent/workflow,audit --kind agent/root 用)。

    Returns:
        (name, description, full_content, frontmatter_dict, body_text)
        name 来自 frontmatter(切片身份,--kind 拿它当 target),与文件名无关。

    Raises:
        SkillParseError: If the file is missing or malformed.
    """
    # 直传 .md 文件 → 读它本身(SKILL.md 或 agent/workflow 声明);目录 → 找 <dir>/SKILL.md。
    if skill_path.is_file():
        skill_file = skill_path
    else:
        skill_file = skill_path / "SKILL.md"
    if not skill_file.exists():
        # 目录里没 SKILL.md:若该目录有别的 .md(agent/workflow 常是目录里的扁平单文件,
        # 真实布局如 agents/developer.md),提示传具体文件——否则"SKILL.md not found"
        # 听着像"你该加个 SKILL.md",但真意是"该指向具体声明文件"。
        if skill_path.is_dir():
            md_files = sorted(skill_path.glob("*.md"))
            if md_files:
                raise SkillParseError(
                    f"{skill_path} 下无 SKILL.md。agent / workflow 声明常是目录里的扁平 .md 文件,"
                    f"请直接传具体文件路径,如:{skill_path / md_files[0].name}"
                )
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
