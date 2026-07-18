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


def test_merge_same_task_evals_collapses_identical_prompt():
    """单 transcript 挖出多个同 prompt eval(同任务多细则)→ 合并成一题多断言,
    省 spawn、去冗余(避免同一任务 spawn 6 次)。不同 prompt 不合并。"""
    from skill_eval.models import EvalSet, EvalCase, Assertion, AssertionType, FixtureFile
    evals = [
        EvalCase(id=1, prompt="分析 app.log 写 report", level="L1",
                 expected_output="含 total_errors", expectations=["要 total_errors"],
                 assertions=[], fixtures=[FixtureFile(path="app.log", content="E1")]),
        EvalCase(id=2, prompt="分析 app.log 写 report", level="L1",
                 expected_output="认 E-AUTH-001", expectations=["E-AUTH-001"],
                 assertions=[], fixtures=[FixtureFile(path="app.log", content="E1")]),
        EvalCase(id=3, prompt="分析 app.log 写 report", level="L2",
                 expected_output="不编造码", expectations=["码白名单"],
                 assertions=[Assertion(name="check", type=AssertionType.CUSTOM_SCRIPT, params={"script": "c.py"})],
                 fixtures=[FixtureFile(path="app.log", content="E1"), FixtureFile(path="c.py", content="#")]),
        EvalCase(id=4, prompt="另一个不同任务", level="L1", expectations=["别的"]),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    # 同 prompt 的 1/2/3 合并成 1;不同 prompt 的 4 保留 → 共 2 个 eval
    assert len(out.evals) == 2
    merged = out.evals[0]
    assert merged.prompt == "分析 app.log 写 report"
    assert merged.level == "L2"                 # 有 fixture → 执行型
    assert len(merged.expectations) == 3        # 三条细则并集
    assert len(merged.assertions) == 1          # CUSTOM_SCRIPT 并集
    assert len(merged.fixtures) == 2            # app.log 去重 + c.py
    assert any(f.startswith("merged:") for f in (merged.flags or []))
    assert out.evals[1].prompt == "另一个不同任务"   # 不同 prompt 不合并
    assert not any(f.startswith("merged:") for f in (out.evals[1].flags or []))


def test_merge_no_op_when_all_prompts_distinct():
    """所有 eval prompt 各不同 → 无合并,原样返回(仅重排 id)。"""
    from skill_eval.models import EvalSet, EvalCase
    es = EvalSet(skill_name="s", evals=[
        EvalCase(id=1, prompt="任务A"), EvalCase(id=2, prompt="任务B")])
    out = EvalInitGenerator._merge_same_task_evals(es)
    assert len(out.evals) == 2
    assert not any(f.startswith("merged:") for e in out.evals for f in (e.flags or []))


# ---------------------------------------------------------------------------
# 合并一致性门:确定性冲突 → 不合并(各 eval 独立保留 + split flag)
# 治本:transcript 挖掘常给同 prompt 出多套互斥 rubric(成功段/失败段两套 ground truth),
# 旧逻辑按 prompt 无条件 union → 自相矛盾题(同一 app.log 不可能既是 E1 又是 E2)。
# ---------------------------------------------------------------------------

def test_merge_same_task_evals_split_on_fixture_conflict():
    """Gate 1:同 prompt 但同 path 不同 content(不同 scenario)→ 不合并。
    复现 eval-3 根因:LLM 给同 prompt 出了 fixture 互斥的多题。"""
    evals = [
        EvalCase(id=1, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="E-AUTH-001 login failed")]),
        EvalCase(id=2, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="E-DB-002 query error")]),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2                       # 不合并:两条独立题
    assert not any(f.startswith("merged:") for e in out.evals for f in (e.flags or []))
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)
    # 各自的 fixture 原样保留(没被 union 混在一起)
    contents = sorted(fx.content for e in out.evals for fx in e.fixtures)
    assert contents == ["E-AUTH-001 login failed", "E-DB-002 query error"]


def test_merge_same_task_evals_split_on_contains_vs_not_contains():
    """Gate 2a:同 prompt、同 fixture(隔离掉 Gate 1),CONTAINS 字面量命中 NOT_CONTAINS 正则 → 矛盾。
    (transcript 出题:同任务既挖到'要含 E-AUTH-001'又挖到'不许含错误码正则'。)"""
    evals = [
        EvalCase(id=1, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="has", type=AssertionType.CONTAINS_PATTERN,
                                       params={"pattern": "E-AUTH-001", "path": "report.md"})]),
        EvalCase(id=2, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="no", type=AssertionType.NOT_CONTAINS,
                                       params={"pattern": r"E-[A-Z]+-\d+", "path": "report.md"})]),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)


def test_merge_same_task_evals_split_on_same_key_different_constant():
    """Gate 2b:同 (file,key) 的 CONTAINS_PATTERN 钉成不同常量 → 矛盾。
    (transcript 出题:成功段挖到 total_errors=0、失败段挖到 total_errors=3。)"""
    evals = [
        EvalCase(id=1, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="zero", type=AssertionType.CONTAINS_PATTERN,
                                       params={"pattern": '"total_errors": 0', "path": "report.json"})]),
        EvalCase(id=2, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="three", type=AssertionType.CONTAINS_PATTERN,
                                       params={"pattern": '"total_errors": 3', "path": "report.json"})]),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)


def test_merge_does_not_flag_regex_contains_as_conflict():
    """保守:CONTAINS_PATTERN 本身是正则(含元字符)→ 证不了字面重叠,跳过,不误判冲突。
    两条 otherwise 一致的同 prompt eval(一条 CONTAINS 正则、一条 NOT_CONTAINS 不相干)→ 仍合并。"""
    evals = [
        EvalCase(id=1, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="re", type=AssertionType.CONTAINS_PATTERN,
                                       params={"pattern": r"E-\d+", "path": "report.md"})]),
        EvalCase(id=2, prompt="分析 app.log 写报告", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="x")],
                 assertions=[Assertion(name="no", type=AssertionType.NOT_CONTAINS,
                                       params={"pattern": "TODO", "path": "report.md"})]),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 1                       # 一致 → 合并
    assert any(f.startswith("merged:") for f in (out.evals[0].flags or []))


def test_merge_partial_conflict_only_splits_conflicting_group():
    """一组同 prompt 冲突(拆)、一组同 prompt 一致(合)、一条不同 prompt(原样)→ 三类各得其所;
    id 全局连续重排(拆出的多条也要各占一个 id)。"""
    evals = [
        # 组 A:冲突(同 path 异 content)→ 拆成 2 条
        EvalCase(id=1, prompt="任务一", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="A1")]),
        EvalCase(id=2, prompt="任务一", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="A2")]),
        # 组 B:一致(同 path 同 content)→ 合成 1 条
        EvalCase(id=3, prompt="任务二", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="B")]),
        EvalCase(id=4, prompt="任务二", level="L2",
                 fixtures=[FixtureFile(path="app.log", content="B")]),
        # 组 C:不同 prompt → 原样
        EvalCase(id=5, prompt="任务三", level="L1"),
    ]
    out = EvalInitGenerator._merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 4                       # 组 A 拆 2 + 组 B 合 1 + 组 C 1
    assert [e.id for e in out.evals] == [1, 2, 3, 4] # id 连续
    prompts = [e.prompt for e in out.evals]
    assert prompts.count("任务一") == 2              # 拆开的两条都还在
    assert prompts.count("任务二") == 1              # 合并
    a_flags = [any(f.startswith("split:") for f in (e.flags or []))
               for e in out.evals if e.prompt == "任务一"]
    assert a_flags == [True, True]
    b = next(e for e in out.evals if e.prompt == "任务二")
    assert any(f.startswith("merged:") for f in (b.flags or []))


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


# ---------------------------------------------------------------------------
# 方向 B:把 skill 声明的产物纳入 mining manifest
# (原则性客观断言源自 skill、transcript 无关;违规 transcript 不再倒扣)
# ---------------------------------------------------------------------------

class TestExtractSkillOutputs:
    """_extract_skill_outputs:确定性抽 SKILL.md 声明的【产物】文件名,不含输入/参考资料。"""

    def test_output_section_filenames_admitted(self):
        body = "## 输出\n\nerror-report.json 含 total_errors 整数、errors 列表。\n"
        assert EvalInitGenerator()._extract_skill_outputs(body) == {"error-report.json"}

    def test_excludes_references_and_read_only_inputs(self):
        body = (
            "## 步骤\n\n"
            "1. 读取 references/error-codes.md 做分类。\n"
            "2. 写出 error-report.json。\n\n"
            "## 参考资料\n\n"
            "权威表在 references/error-codes.md;安全规则在 references/security-policy.md。\n\n"
            "## 输出\n\nerror-report.json 含 total_errors。\n"
        )
        out = EvalInitGenerator()._extract_skill_outputs(body)
        assert "error-report.json" in out
        assert "error-codes.md" not in out
        assert "security-policy.md" not in out
        assert not any("error-codes" in p or "security-policy" in p for p in out)

    def test_write_verb_in_steps_admits_without_output_section(self):
        body = "## 步骤\n\n3. 生成 report.json 保存结果。\n"
        assert "report.json" in EvalInitGenerator()._extract_skill_outputs(body)

    def test_read_verb_in_steps_not_admitted(self):
        body = "## 步骤\n\n1. 读取 input.csv 做处理。\n"
        assert EvalInitGenerator()._extract_skill_outputs(body) == set()

    def test_no_signal_returns_empty(self):
        body = "## 规则\n\n禁止编造错误码。\n## 背景\n\n见 error-codes.md。\n"
        assert EvalInitGenerator()._extract_skill_outputs(body) == set()


def test_from_transcript_keeps_skill_output_assertion_on_violation_transcript(monkeypatch, tmp_path):
    """B 端到端:违规 transcript(只读没写)没产 error-report.json,但 skill '## 输出' 声明它 →
    mining 应保住 FILE_EXISTS error-report.json(改前因 closed-set manifest 把它 drop)。
    原则性断言 transcript 无关、源自 skill。"""
    import json
    gen = EvalInitGenerator()
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        {"message": {"role": "user", "content": "分析 logs/app.log 写 error-report.json"}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": "/ws/logs/app.log"}}]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": "E-AUTH-001 login failed"}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "我不写 error-report.json 了,直接总结。"}]}},
    ]), encoding="utf-8")
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: log\ndescription: d\n---\n# 日志分析\n\n"
        "## 步骤\n\n1. 读取 app.log。\n2. 写出 error-report.json。\n\n"
        "## 输出\n\nerror-report.json 含 total_errors。\n", encoding="utf-8")
    fake = json.dumps({"skill_name": "log", "evals": [{"id": 1, "name": "L2", "level": "L2",
        "prompt": "分析 app.log 写 error-report.json", "expected_output": "", "expectations": [],
        "assertions": [{"name": "report exists", "type": "FILE_EXISTS",
                        "params": {"filename": "error-report.json"}}],
        "fixtures": []}]})
    monkeypatch.setattr("skill_eval.evals.generator.call_claude", lambda *a, **k: fake)
    es = gen.generate_from_transcript(sp, tp)
    fns = [a.params.get("filename") for a in es.evals[0].assertions
           if a.type == AssertionType.FILE_EXISTS]
    assert "error-report.json" in fns            # 保住了(改前被 drop)


# ---------------------------------------------------------------------------
# mining --by-skill:多 skill transcript 切分(平行 audit --by-skill)
# ---------------------------------------------------------------------------

def _multi_skill_transcript(tmp_path: Path, target: str = "skillA", other: str = "skillB") -> Path:
    """两 skill 交替的 transcript:target 写 target.json、other 写 other.json。"""
    import json
    tp = tmp_path / "multi.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        {"message": {"role": "user", "content": "do A then B"}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": target}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w1", "name": "Write",
             "input": {"file_path": f"/p/{target}.json", "content": "a"}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "s2", "name": "Skill", "input": {"skill": other}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w2", "name": "Write",
             "input": {"file_path": f"/p/{other}.json", "content": "b"}}]}},
    ]), encoding="utf-8")
    return tp


def test_invoked_skills_lists_all_in_order(tmp_path: Path):
    from skill_eval.audit.slice import invoked_skills
    tp = _multi_skill_transcript(tmp_path)
    assert invoked_skills(tp) == ["skillA", "skillB"]


def test_from_transcript_by_skill_isolates_target_segment(monkeypatch, tmp_path: Path):
    """--by-skill:只挖本 skill 段 → manifest 只含本 skill 文件,别 skill 的断言被 drop。
    对照:不切分(默认)会把别 skill 文件也当本 skill → 串味(两者都留)。"""
    import json
    gen = EvalInitGenerator()
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: skillA\ndescription: d\n---\n# b\n", encoding="utf-8")
    tp = _multi_skill_transcript(tmp_path)
    fake = json.dumps({"skill_name": "skillA", "evals": [{"id": 1, "name": "L2", "level": "L2",
        "prompt": "p", "expected_output": "", "expectations": [],
        "assertions": [{"name": "A", "type": "FILE_EXISTS", "params": {"filename": "skillA.json"}},
                       {"name": "B", "type": "FILE_EXISTS", "params": {"filename": "skillB.json"}}],
        "fixtures": []}]})
    monkeypatch.setattr("skill_eval.evals.generator.call_claude", lambda *a, **k: fake)
    # by_skill=True:只切 skillA 段 → skillA.json 留、skillB.json drop
    es = gen.generate_from_transcript(sp, tp, by_skill=True)
    fns = [a.params.get("filename") for a in es.evals[0].assertions if a.type == AssertionType.FILE_EXISTS]
    assert "skillA.json" in fns and "skillB.json" not in fns
    # 对照 by_skill=False:整份 transcript,skillB.json 也在 manifest → 留(串味)
    es2 = gen.generate_from_transcript(sp, tp, by_skill=False)
    fns2 = [a.params.get("filename") for a in es2.evals[0].assertions if a.type == AssertionType.FILE_EXISTS]
    assert "skillA.json" in fns2 and "skillB.json" in fns2


def test_from_transcript_by_skill_target_never_invoked_raises(tmp_path: Path):
    """--by-skill 但本 skill 从未在 transcript 被调用 → 明确报错(别静默挖空)。"""
    from skill_eval.errors import SkillEvalError
    gen = EvalInitGenerator()
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: skillC\ndescription: d\n---\n# b\n", encoding="utf-8")
    tp = _multi_skill_transcript(tmp_path)  # 含 skillA + skillB,无 skillC
    with pytest.raises(SkillEvalError):
        gen.generate_from_transcript(sp, tp, by_skill=True)
