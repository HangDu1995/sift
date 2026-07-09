"""Tests for eval generator enhancements — domain-aware assertions + mode support."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_eval.config.loader import EvalConfig
from skill_eval.evals.generator import EvalInitGenerator
from skill_eval.models import EvalCase


def _make_skill(tmp_path: Path, name: str = "test-skill", domain: str = "general",
                modes: list[str] | None = None, body: str = "") -> Path:
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = {"name": name, "description": f"A {domain} skill", "type": "domain", "domain": domain}
    if modes:
        fm["modes"] = modes
    fm_str = yaml.dump(fm, allow_unicode=True)
    content = f"---\n{fm_str}---\n\n# Overview\n\n{body}\n"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestDomainAwareAssertions:
    def test_injects_domain_template_assertions(self, tmp_path: Path):
        config = EvalConfig(domain_templates={
            "api": [
                {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "has router"},
                {"type": "NOT_CONTAINS", "pattern": "TODO", "description": "no todos"},
            ],
        })
        gen = EvalInitGenerator(config=config)
        skill_dir = _make_skill(tmp_path, domain="api", body="- Step 1: Build the API\n- Step 2: Test it")
        result = gen.generate_template(skill_dir)
        all_types = [a.type.value for e in result.evals for a in e.assertions]
        assert "CONTAINS_PATTERN" in all_types

    def test_no_config_no_domain_injection(self, tmp_path: Path):
        gen = EvalInitGenerator()
        skill_dir = _make_skill(tmp_path, body="- Do something\n- Check result")
        result = gen.generate_template(skill_dir)
        assert len(result.evals) >= 1


class TestModeSupport:
    def test_multi_mode_generates_per_mode(self, tmp_path: Path):
        gen = EvalInitGenerator(modes=["create", "refactor"])
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        modes_found = {e.mode for e in result.evals if e.mode}
        assert "create" in modes_found
        assert "refactor" in modes_found

    def test_no_modes_default(self, tmp_path: Path):
        gen = EvalInitGenerator()
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        assert all(e.mode == "" for e in result.evals)

    def test_mode_passed_to_llm_prompt(self, tmp_path: Path):
        gen = EvalInitGenerator(modes=["create"])
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        assert any(e.mode == "create" for e in result.evals)


def test_evalcase_flags_default_empty():
    ev = EvalCase(id=1, prompt="x")
    assert ev.flags == []
