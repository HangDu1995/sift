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


def test_resolve_skill_dir_non_skill_md_file_kept(tmp_path: Path):
    """非 SKILL.md 的 .md 文件(agent/workflow 声明)→ 原样保留,不转父目录。
    resolve_skill_dir 只规范化"误传 SKILL.md"这一种;agent 声明文件是故意的输入,不动它。"""
    agents = tmp_path / "agents"
    agents.mkdir()
    dev = agents / "developer.md"
    dev.write_text("---\nname: developer\ndescription: d\n---\nbody", encoding="utf-8")
    assert resolve_skill_dir(dev) == dev              # 不转成 agents/


def test_parse_skill_md_accepts_arbitrary_md_file(tmp_path: Path):
    """parse_skill_md 接受任意 .md 文件(agent/workflow 声明),直读其 frontmatter+body。
    身份靠 frontmatter name(--kind 拿它当切片 target),不靠文件名。"""
    agents = tmp_path / "agents"
    agents.mkdir()
    dev = agents / "developer.md"
    dev.write_text("---\nname: developer\ndescription: 开发执行器\n---\n# developer\n角色声明\n",
                   encoding="utf-8")
    name, desc, _content, _fm, body = parse_skill_md(dev)
    assert name == "developer"                        # frontmatter name = 切片身份
    assert "角色声明" in body
    assert "developer.md" not in body                 # 读的是 developer.md 本身


def test_parse_skill_md_agent_md_equals_materialized_skill_md(tmp_path: Path):
    """直接读 agent .md == 物化成 <dir>/SKILL.md 再读(audit 对账零影响的根)。
    audit 只吃 name(切片 target)+ body(声明);两者全等 → 切段相同、声明相同 → 对账逐字节相同。"""
    agents = tmp_path / "agents"
    agents.mkdir()
    dev_md = agents / "developer.md"
    dev_md.write_text("---\nname: developer\ndescription: 角色\n---\n# developer\n声明正文\n",
                      encoding="utf-8")
    name_y, desc_y, _c, fm_y, body_y = parse_skill_md(dev_md)             # 路径Y:直接读
    devdir = tmp_path / "dev"
    devdir.mkdir()
    (devdir / "SKILL.md").write_text(dev_md.read_text(encoding="utf-8"), encoding="utf-8")
    name_x, desc_x, _c, fm_x, body_x = parse_skill_md(devdir)             # 路径X:物化 SKILL.md
    assert (name_y, desc_y, body_y, fm_y) == (name_x, desc_x, body_x, fm_x)


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


def test_parse_skill_md_dir_with_md_but_no_skill_md_hints_file(tmp_path: Path):
    """目录里有 .md 但没 SKILL.md → 错误提示传具体文件(而非"该加 SKILL.md")。

    真实 agent 布局(agents/developer.md + reviewer.md + …)误传共享目录时给正确引导:
    "SKILL.md not found" 会误导成"补一个 SKILL.md",真意是"指向具体声明文件"。
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "developer.md").write_text("---\nname: developer\ndescription: d\n---\nbody", encoding="utf-8")
    (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: r\n---\nbody", encoding="utf-8")
    with pytest.raises(SkillParseError, match="直接传具体文件路径") as exc_info:
        parse_skill_md(agents)
    # 提示里点出一个真实存在的 .md(sorted 首个 = developer.md)
    assert "developer.md" in str(exc_info.value)


def test_parse_skill_md_empty_dir_still_generic_not_found(tmp_path: Path):
    """空目录(无任何 .md)→ 仍走通用 "SKILL.md not found"(无可提示的具体文件)。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SkillParseError, match="SKILL.md not found"):
        parse_skill_md(empty)


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
