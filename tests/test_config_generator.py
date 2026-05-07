"""Tests for LLM config generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skill_eval.config.generator import ConfigGenerator, scan_skills_metadata


def _make_skill(root: Path, collection: str, skill_name: str, frontmatter: dict | None = None):
    col_dir = root / collection / "skills" / skill_name
    col_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        fm = yaml.dump(frontmatter, allow_unicode=True)
        content = f"---\n{fm}---\n\n# Body\n"
    else:
        content = "# No frontmatter\n"
    (col_dir / "SKILL.md").write_text(content, encoding="utf-8")


class TestScanSkillsMetadata:
    def test_scans_multiple_collections(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-core", "skill-a", {
            "name": "skill-a", "type": "domain", "domain": "api",
            "description": "Build APIs", "tags": ["rest"],
        })
        _make_skill(tmp_path, "mylib-ext", "skill-b", {
            "name": "skill-b", "type": "tool", "domain": "general",
            "description": "Utility tool",
        })
        result = scan_skills_metadata(tmp_path)
        assert result["total_skills"] == 2
        assert "api" in result["domains"]
        assert "tool" in result["types"]
        assert len(result["collection_prefixes"]) >= 1

    def test_empty_dir(self, tmp_path: Path):
        result = scan_skills_metadata(tmp_path)
        assert result["total_skills"] == 0

    def test_extracts_metadata(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-skills", "my-skill", {
            "name": "my-skill", "type": "domain", "domain": "ui",
            "description": "A UI skill", "tags": ["react", "component"],
        })
        result = scan_skills_metadata(tmp_path)
        skill = result["skills"][0]
        assert skill["name"] == "my-skill"
        assert skill["domain"] == "ui"
        assert skill["tags"] == ["react", "component"]


class TestConfigGeneratorBuildPrompt:
    def test_prompt_contains_domains(self):
        gen = ConfigGenerator()
        meta = {
            "total_skills": 2,
            "domains": ["api", "ui"],
            "types": ["tool"],
            "collection_prefixes": ["mylib-*"],
            "skills": [
                {"name": "s1", "type": "tool", "domain": "api", "description": "d"},
                {"name": "s2", "type": "tool", "domain": "ui", "description": "d"},
            ],
        }
        prompt = gen.build_prompt(meta)
        assert "api" in prompt
        assert "ui" in prompt
        assert "mylib-*" in prompt

    def test_prompt_includes_assertion_types(self):
        gen = ConfigGenerator()
        meta = {"total_skills": 0, "domains": [], "types": [],
                "collection_prefixes": [], "skills": []}
        prompt = gen.build_prompt(meta)
        assert "FILE_EXISTS" in prompt or "CONTAINS_PATTERN" in prompt

    def test_prompt_contains_skill_data(self):
        gen = ConfigGenerator()
        meta = {
            "total_skills": 1,
            "domains": ["api"],
            "types": ["domain"],
            "collection_prefixes": [],
            "skills": [{"name": "api-builder", "type": "domain", "domain": "api",
                         "description": "Build REST APIs", "tags": []}],
        }
        prompt = gen.build_prompt(meta)
        assert "api-builder" in prompt


class TestCleanYamlOutput:
    def test_strips_yaml_fence(self):
        from skill_eval.config.generator import _clean_yaml_output
        raw = "```yaml\nfoo: bar\n```"
        assert _clean_yaml_output(raw) == "foo: bar"

    def test_strips_plain_fence(self):
        from skill_eval.config.generator import _clean_yaml_output
        raw = "```\nfoo: bar\n```"
        assert _clean_yaml_output(raw) == "foo: bar"

    def test_no_fence(self):
        from skill_eval.config.generator import _clean_yaml_output
        raw = "foo: bar"
        assert _clean_yaml_output(raw) == "foo: bar"
