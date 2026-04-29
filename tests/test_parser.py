"""Tests for SKILL.md parser."""

from __future__ import annotations

import pytest
from pathlib import Path

from skill_eval.parser import parse_skill_md
from skill_eval.errors import SkillParseError


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
