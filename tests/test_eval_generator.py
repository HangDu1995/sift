"""Tests for eval generator enhancements — domain-aware assertions + mode support."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_eval.config.loader import EvalConfig
from skill_eval.evals.generator import EvalInitGenerator
from skill_eval.evals.transcript import SessionDigest, FileContent
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


def test_build_transcript_prompt_contains_manifest_schema_and_trapA():
    gen = EvalInitGenerator()
    digest = SessionDigest(
        user_turns=["造一个 cli"],
        input_files=[FileContent(path="calc.py", content="x=1")],
        produced_files=[FileContent(path="sub/cli.py", content="print(1)")],
    )
    prompt = gen._build_transcript_prompt("calc", "a skill", "body", digest)
    assert "sub/cli.py" in prompt and "calc.py" in prompt   # manifest
    assert '"filename"' in prompt and '"path"' in prompt    # schema pin
    assert "CUSTOM_SCRIPT" in prompt
    assert "not ground truth" in prompt                     # trap-A


def test_generate_from_transcript_pipeline(monkeypatch, tmp_path):
    gen = EvalInitGenerator()
    # 假 transcript: 一个 Write
    import json
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        {"message": {"role": "user", "content": "造 cli"}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w1", "name": "Write",
             "input": {"file_path": "/proj/cli.py", "content": "from calc import evaluate\n"}}]}},
    ]), encoding="utf-8")
    # mock LLM 返回带裸 filename 的 eval(应被 _enforce_manifest 处理)
    fake = json.dumps({"skill_name": "calc", "evals": [{"id": 1, "name": "L2", "level": "L2",
        "prompt": "p", "expected_output": "", "expectations": [],
        "assertions": [{"name": "cli", "type": "FILE_EXISTS", "params": {"filename": "cli.py"}}],
        "fixtures": []}]})
    monkeypatch.setattr("skill_eval.evals.generator.call_claude", lambda *a, **k: fake)
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: calc\ndescription: d\n---\n# body\n", encoding="utf-8")
    es = gen.generate_from_transcript(sp, tp)
    # manifest 含 cli.py(单文件->parent /proj 作 root),裸名保持;pipeline 跑通即过
    assert es.evals[0].assertions[0].params["filename"] == "cli.py"


def test_generate_from_transcript_heartbeat_during_slow_call(monkeypatch, tmp_path):
    """mining 单次慢 LLM 调用:心跳每 N 秒报'仍在挖',避免 Mining… 后长时间静默
    (同 audit/path-1 进度诉求)。monkeypatch 间隔到极小 + call_claude sleep,验心跳触发。"""
    import json as _json
    import time
    gen = EvalInitGenerator()
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(_json.dumps(r) for r in [
        {"message": {"role": "user", "content": "造 cli"}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w1", "name": "Write",
             "input": {"file_path": "/proj/cli.py", "content": "x"}}]}},
    ]), encoding="utf-8")
    fake = _json.dumps({"skill_name": "calc", "evals": [{"id": 1, "name": "L2", "level": "L2",
        "prompt": "p", "expected_output": "", "expectations": [],
        "assertions": [{"name": "cli", "type": "FILE_EXISTS", "params": {"filename": "cli.py"}}],
        "fixtures": []}]})
    monkeypatch.setattr("skill_eval.evals.generator._HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr("skill_eval.evals.generator.call_claude",
                        lambda *a, **k: (time.sleep(0.2), fake)[1])
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: calc\ndescription: d\n---\n# body\n", encoding="utf-8")
    beats = []
    gen.generate_from_transcript(sp, tp, on_heartbeat=beats.append)
    assert len(beats) >= 1            # 慢调用期间心跳至少触发一次
    assert "仍在调用 LLM" in beats[0]


def test_enforce_keeps_pathless_contains_pattern():
    ev = EvalCase(id=1, prompt="x", level="L1", assertions=[
        Assertion(name="k", type=AssertionType.CONTAINS_PATTERN, params={"pattern": "keyword"})])  # no path
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"sub/cli.py"})
    assert len(out.evals[0].assertions) == 1        # kept, not dropped
    assert out.evals[0].flags == []                 # no drop flag

def test_enforce_rewrites_json_has_field_filename():
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="f", type=AssertionType.JSON_HAS_FIELD,
                  params={"filename": "config.json", "field": "a.b"})])
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"sub/config.json"})
    assert out.evals[0].assertions[0].params["filename"] == "sub/config.json"
    assert out.evals[0].assertions[0].params["field"] == "a.b"   # untouched

def test_enforce_drops_custom_script_resolves_but_no_fixture():
    # script basename resolves to a manifest path, but eval ships no such fixture -> drop
    ev = EvalCase(id=1, prompt="x", level="L2", assertions=[
        Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})],
        fixtures=[])
    out = EvalInitGenerator._enforce_manifest(_es(ev), {"sub/check.py"})  # manifest has it, fixture doesn't
    assert out.evals[0].assertions == []
    assert out.evals[0].flags

def test_generate_from_transcript_missing_file_raises(tmp_path):
    import pytest
    from skill_eval.errors import SkillEvalError
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
    with pytest.raises(SkillEvalError):
        EvalInitGenerator().generate_from_transcript(sp, tmp_path / "nope.jsonl")
