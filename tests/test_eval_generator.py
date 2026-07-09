"""Tests for eval generator enhancements — domain-aware assertions + mode support."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_eval.config.loader import EvalConfig
from skill_eval.evals.generator import EvalInitGenerator
from skill_eval.models import Assertion, AssertionType, EvalCase, EvalSet, FixtureFile


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


def _es(ev): return EvalSet(skill_name="s", evals=[ev])

def test_enforce_rewrites_bare_filename_to_manifest_path():
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="cli", type=AssertionType.FILE_EXISTS, params={"filename": "cli.py"})])
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"sub/cli.py", "calc.py"})
    assert out.evals[0].assertions[0].params["filename"] == "sub/cli.py"

def test_enforce_drops_custom_script_without_fixture():
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})],
        fixtures=[])   # checker 没 ship
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"cli.py"})
    assert out.evals[0].assertions == []
    assert out.evals[0].flags  # 有标记

def test_enforce_keeps_custom_script_when_fixture_shipped():
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})],
        fixtures=[FixtureFile(path="check.py", content="")])
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"cli.py", "check.py"})
    assert len(out.evals[0].assertions) == 1

def test_enforce_scopes_contains_pattern_path():
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="imp", type=AssertionType.CONTAINS_PATTERN,
                  params={"pattern": "import", "path": "cli.py"})])
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"sub/cli.py"})
    assert out.evals[0].assertions[0].params["path"] == "sub/cli.py"
