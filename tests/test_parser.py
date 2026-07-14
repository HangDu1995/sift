"""Tests for SKILL.md parser."""

from __future__ import annotations

import pytest
from pathlib import Path

from skill_eval.parser import parse_skill_md, resolve_skill_dir
from skill_eval.errors import SkillParseError


def test_resolve_skill_dir_directory(tmp_path: Path):
    """传目录 → 原样返回。"""
    assert resolve_skill_dir(tmp_path) == tmp_path


def test_resolve_skill_dir_skill_md_file(tmp_path: Path):
    """传 SKILL.md 文件 → 返回其父目录(规范化)。"""
    (tmp_path / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\nbody", encoding="utf-8")
    assert resolve_skill_dir(tmp_path / "SKILL.md") == tmp_path


def test_resolve_skill_dir_nonexistent_passthrough(tmp_path: Path):
    """不存在的路径(非文件)→ 原样,交给下游报 not found。"""
    ghost = tmp_path / "nope"
    assert resolve_skill_dir(ghost) == ghost


def test_parse_skill_md_accepts_skill_md_file(tmp_path: Path):
    """parse_skill_md 接受 SKILL.md 文件路径(内部规范化取父目录),不只目录。"""
    (tmp_path / "SKILL.md").write_text(
        "---\nname: filearg\ndescription: does thing\n---\nbody", encoding="utf-8"
    )
    name, *_ = parse_skill_md(tmp_path / "SKILL.md")
    assert name == "filearg"


def test_parse_skill_md_success(sample_skill_path: Path):
    name, description, content, frontmatter, body = parse_skill_md(sample_skill_path)
    assert name == "sample-skill"
    assert "data transformation" in description.lower()
    assert "---" in content
    assert isinstance(frontmatter, dict)
    assert frontmatter["name"] == "sample-skill"
    assert len(body) > 0
    assert "Workflow" in body


def test_parse_skill_md_missing_file(tmp_path: Path):
    with pytest.raises(SkillParseError, match="SKILL.md not found"):
        parse_skill_md(tmp_path)


def test_parse_skill_md_no_frontmatter(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("Just some content without frontmatter", encoding="utf-8")
    with pytest.raises(SkillParseError, match="no opening ---"):
        parse_skill_md(tmp_path)


def test_parse_skill_md_unclosed_frontmatter(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nname: test\n", encoding="utf-8")
    with pytest.raises(SkillParseError, match="no closing ---"):
        parse_skill_md(tmp_path)


def test_parse_skill_md_invalid_yaml(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("---\nname: [invalid yaml\n---\nbody", encoding="utf-8")
    with pytest.raises(SkillParseError, match="Invalid YAML"):
        parse_skill_md(tmp_path)


def test_parse_skill_md_multiline_description(tmp_path: Path):
    content = "---\nname: my-skill\ndescription: |\n  Line one\n  Line two\n---\n\nBody here\n"
    (tmp_path / "SKILL.md").write_text(content, encoding="utf-8")
    name, description, _, frontmatter, body = parse_skill_md(tmp_path)
    assert name == "my-skill"
    assert "Line one" in description
    assert "Body here" in body


def test_parse_skill_md_quoted_description(tmp_path: Path):
    content = '---\nname: my-skill\ndescription: "A quoted description"\n---\n\nBody\n'
    (tmp_path / "SKILL.md").write_text(content, encoding="utf-8")
    name, description, _, _, _ = parse_skill_md(tmp_path)
    assert "quoted description" in description
