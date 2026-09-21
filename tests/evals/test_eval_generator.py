"""Tests for eval generator enhancements — domain-aware assertions + mode support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from sift.config.loader import EvalConfig
from sift.evals.generator import EvalInitGenerator, PromptSource
from sift.evals.transcript import FileContent, SessionDigest
from sift.evals.transforms import (
    build_agent_prompt,
    build_knowledge_prompt,
    build_llm_prompt,
    build_procedural_prompt,
    build_transcript_prompt,
    collect_unknown_field_warnings,
    enforce_manifest,
    extract_skill_outputs,
    focus_directive,
    merge_same_task_evals,
    parse_eval_response,
    supplement_directive,
)
from sift.models import Assertion, AssertionType, EvalCase, EvalSet, FixtureFile


def _make_skill(
    tmp_path: Path, name: str = "test-skill", domain: str = "general", modes: list[str] | None = None, body: str = ""
) -> Path:
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
    @staticmethod
    def test_injects_domain_template_assertions(tmp_path: Path):
        config = EvalConfig(
            domain_templates={
                "api": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "has router"},
                    {"type": "NOT_CONTAINS", "pattern": "TODO", "description": "no todos"},
                ],
            }
        )
        gen = EvalInitGenerator(config=config)
        skill_dir = _make_skill(tmp_path, domain="api", body="- Step 1: Build the API\n- Step 2: Test it")
        result = gen.generate_template(skill_dir)
        all_types = [a.type.value for e in result.evals for a in e.assertions]
        assert "CONTAINS_PATTERN" in all_types

    @staticmethod
    def test_no_config_no_domain_injection(tmp_path: Path):
        gen = EvalInitGenerator()
        skill_dir = _make_skill(tmp_path, body="- Do something\n- Check result")
        result = gen.generate_template(skill_dir)
        assert len(result.evals) >= 1


class TestModeSupport:
    @staticmethod
    def test_multi_mode_generates_per_mode(tmp_path: Path):
        gen = EvalInitGenerator(modes=["create", "refactor"])
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        modes_found = {e.mode for e in result.evals if e.mode}
        assert "create" in modes_found
        assert "refactor" in modes_found

    @staticmethod
    def test_no_modes_default(tmp_path: Path):
        gen = EvalInitGenerator()
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        assert all(e.mode == "" for e in result.evals)

    @staticmethod
    def test_mode_passed_to_llm_prompt(tmp_path: Path):
        gen = EvalInitGenerator(modes=["create"])
        skill_dir = _make_skill(tmp_path, body="- Step 1\n- Step 2")
        result = gen.generate_template(skill_dir)
        assert any(e.mode == "create" for e in result.evals)


def test_evalcase_flags_default_empty():
    ev = EvalCase(id=1, prompt="x")
    assert ev.flags == []


def _es(ev):
    return EvalSet(skill_name="s", evals=[ev])


def test_enforce_rewrites_bare_filename_to_manifest_path():
    ev = EvalCase(
        **_eval_case_values(
            assertions=[Assertion(name="cli", type=AssertionType.FILE_EXISTS, params={"filename": "cli.py"})]
        )
    )
    out = enforce_manifest(_es(ev), {"sub/cli.py", "calc.py"})
    assert out.evals[0].assertions[0].params["filename"] == "sub/cli.py"


def test_enforce_drops_custom_script_without_fixture():
    ev = EvalCase(
        **_eval_case_values_fixtures(
            assertions=[Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})]
        )
    )  # checker 没 ship
    out = enforce_manifest(_es(ev), {"cli.py"})
    assert out.evals[0].assertions == []
    assert out.evals[0].flags  # 有标记


def test_enforce_keeps_custom_script_when_fixture_shipped():
    ev = EvalCase(
        **_eval_case_values_fixtures(
            assertions=[Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})],
            fixtures=[FixtureFile(path="check.py", content="")],
        )
    )
    out = enforce_manifest(_es(ev), {"cli.py", "check.py"})
    assert len(out.evals[0].assertions) == 1


def test_enforce_scopes_contains_pattern_path():
    ev = EvalCase(
        **_eval_case_values(
            assertions=[
                Assertion(
                    name="imp", type=AssertionType.CONTAINS_PATTERN, params={"pattern": "import", "path": "cli.py"}
                )
            ]
        )
    )
    out = enforce_manifest(_es(ev), {"sub/cli.py"})
    assert out.evals[0].assertions[0].params["path"] == "sub/cli.py"


def test_build_transcript_prompt_contains_manifest_schema_and_trapa():
    digest = SessionDigest(
        user_turns=["造一个 cli"],
        input_files=[FileContent(path="calc.py", content="x=1")],
        produced_files=[FileContent(path="sub/cli.py", content="print(1)")],
    )
    prompt = build_transcript_prompt(
        digest=digest, source=PromptSource(name="calc", description="a skill", content="body")
    )
    assert "sub/cli.py" in prompt and "calc.py" in prompt  # manifest
    assert '"filename"' in prompt and '"path"' in prompt  # schema pin
    assert "CUSTOM_SCRIPT" in prompt
    assert "not ground truth" in prompt  # trap-A


def test_build_transcript_prompt_agent_kind_uses_agent_wording():
    """kind=agent 的 mining prompt 用 agent 导向措辞(角色/system prompt/工具白名单),非 skill。

    agent mining 对称缺口:_build_transcript_prompt 原只 skill 导向,agent 走它措辞失配。
    """
    digest = SessionDigest(
        user_turns=["审查这份 PR diff"],
        input_files=[FileContent(path="src/auth.py", content="AKIA")],
        produced_files=[],
    )
    prompt = build_transcript_prompt(
        digest=digest,
        kind="agent",
        source=PromptSource(
            name="pr-reviewer",
            description="审查 PR diff 给建议",
            content="# 你是审查者\n只读不改",
            frontmatter={"tools": ["Read", "Grep"]},
        ),
    )
    # agent 导向措辞
    assert "Agent system prompt" in prompt
    assert "角色" in prompt
    assert "Read" in prompt and "Grep" in prompt  # 工具白名单喂进 prompt
    # 不含 skill-only 措辞
    assert "Skill 定义" not in prompt
    assert "## SKILL\n" not in prompt
    # rationale 指向 agent 职责,非 SKILL.md
    assert "agent 正文" in prompt
    # 只读 agent 的断言查 transcript(回答文本),不指输入文件 path
    assert "scope" in prompt and "transcript" in prompt


def test_generate_from_transcript_agent_kind_routes_agent_prompt(monkeypatch, tmp_path):
    """generate_from_transcript(kind='agent') → 走 agent 版 prompt(含 agent 措辞,非 skill)。

    验证 kind 从 generate_from_transcript 透到 _build_transcript_prompt 的 agent 分支。
    """
    import json

    gen = EvalInitGenerator()
    tp = tmp_path / "t.jsonl"
    transcript_records = [
        {"agent": "pr-reviewer", "message": {"role": "user", "content": "审查这份 PR diff"}},
        {
            "agent": "pr-reviewer",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "[src/auth.py:42] (🔴) 硬编码 AKIA"}],
            },
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in transcript_records),
        encoding="utf-8",
    )
    captured = {}

    def fake_llm(prompt):
        captured["prompt"] = prompt
        return json.dumps({"skill_name": "pr-reviewer", "evals": []})

    monkeypatch.setattr(gen, "_llm_call", fake_llm)
    sp = tmp_path / "agent.md"
    sp.write_text(
        "---\nname: pr-reviewer\ndescription: 审查 PR diff\ntools:\n  - Read\n  - Grep\n---\n# 你是审查者\n只读不改\n",
        encoding="utf-8",
    )
    gen.generate_from_transcript(sp, tp, kind="agent")
    assert "Agent system prompt" in captured["prompt"]
    assert "Skill 定义" not in captured["prompt"]


def test_generate_from_transcript_pipeline(monkeypatch, tmp_path):
    gen = EvalInitGenerator()
    # 假 transcript: 一个 Write
    import json

    tp = tmp_path / "t.jsonl"
    transcript_records = [
        {"message": {"role": "user", "content": "造 cli"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/proj/cli.py", "content": "from calc import evaluate\n"},
                    }
                ],
            }
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    # mock LLM 返回带裸 filename 的 eval(应被 _enforce_manifest 处理)
    fake = json.dumps(
        {
            "skill_name": "calc",
            "evals": [_generated_eval()],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    sp = _generator_skill(tmp_path, "---\nname: calc\ndescription: d\n---\n# body\n")
    es = gen.generate_from_transcript(sp, tp)
    # manifest 含 cli.py(单文件->parent /proj 作 root),裸名保持;pipeline 跑通即过
    assert es.evals[0].assertions[0].params["filename"] == "cli.py"


def test_generate_from_transcript_heartbeat_during_slow_call(monkeypatch, tmp_path):
    """mining 单次慢 LLM 调用:心跳每 N 秒报'仍在挖',避免 Mining… 后长时间静默
    (同 audit/path-1 进度诉求)。monkeypatch 间隔到极小 + call_claude sleep,验心跳触发。
    """
    import json as _json
    import time

    gen = EvalInitGenerator()
    tp = tmp_path / "t.jsonl"
    transcript_records = _cli_write_records()
    tp.write_text(
        "\n".join(_json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    fake = _json.dumps(
        {
            "skill_name": "calc",
            "evals": [_generated_eval()],
        }
    )
    monkeypatch.setattr("sift.evals.generator._HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: (time.sleep(0.2), fake)[1])
    sp = _generator_skill(tmp_path, "---\nname: calc\ndescription: d\n---\n# body\n")
    beats = []
    gen.generate_from_transcript(sp, tp, on_heartbeat=beats.append)
    assert len(beats) >= 1  # 慢调用期间心跳至少触发一次
    assert "仍在调用 LLM" in beats[0]


def test_merge_same_task_evals_collapses_identical_prompt():
    """单 transcript 挖出多个同 prompt eval(同任务多细则)→ 合并成一题多断言,
    省 spawn、去冗余(避免同一任务 spawn 6 次)。不同 prompt 不合并。
    """

    evals = [
        EvalCase(**_eval_case_values_assertions(fixtures=[FixtureFile(path="app.log", content="E1")])),
        EvalCase(
            **_eval_case_values_assertions(
                id=2,
                expected_output="认 E-AUTH-001",
                expectations=["E-AUTH-001"],
                fixtures=[FixtureFile(path="app.log", content="E1")],
            )
        ),
        EvalCase(
            **_eval_case_values_assertions(
                id=3,
                level="L2",
                expected_output="不编造码",
                expectations=["码白名单"],
                assertions=[Assertion(name="check", type=AssertionType.CUSTOM_SCRIPT, params={"script": "c.py"})],
                fixtures=[FixtureFile(path="app.log", content="E1"), FixtureFile(path="c.py", content="#")],
            )
        ),
        EvalCase(id=4, prompt="另一个不同任务", level="L1", expectations=["别的"]),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    # 同 prompt 的 1/2/3 合并成 1;不同 prompt 的 4 保留 → 共 2 个 eval
    assert len(out.evals) == 2
    merged = out.evals[0]
    assert merged.prompt == "分析 app.log 写 report"
    assert merged.level == "L2"  # 有 fixture → 执行型
    assert len(merged.expectations) == 3  # 三条细则并集
    assert len(merged.assertions) == 1  # CUSTOM_SCRIPT 并集
    assert len(merged.fixtures) == 2  # app.log 去重 + c.py
    assert any(f.startswith("merged:") for f in (merged.flags or []))
    assert out.evals[1].prompt == "另一个不同任务"  # 不同 prompt 不合并
    assert not any(f.startswith("merged:") for f in (out.evals[1].flags or []))


def test_merge_no_op_when_all_prompts_distinct():
    """所有 eval prompt 各不同 → 无合并,原样返回(仅重排 id)。"""

    es = EvalSet(skill_name="s", evals=[EvalCase(id=1, prompt="任务A"), EvalCase(id=2, prompt="任务B")])
    out = merge_same_task_evals(es)
    assert len(out.evals) == 2
    assert not any(f.startswith("merged:") for e in out.evals for f in (e.flags or []))


# ---------------------------------------------------------------------------
# 合并一致性门:确定性冲突 → 不合并(各 eval 独立保留 + split flag)
# 治本:transcript 挖掘常给同 prompt 出多套互斥 rubric(成功段/失败段两套 ground truth),
# 旧逻辑按 prompt 无条件 union → 自相矛盾题(同一 app.log 不可能既是 E1 又是 E2)。
# ---------------------------------------------------------------------------


def test_merge_same_task_evals_split_on_fixture_conflict():
    """Gate 1:同 prompt 但同 path 不同 content(不同 scenario)→ 不合并。
    复现 eval-3 根因:LLM 给同 prompt 出了 fixture 互斥的多题。
    """
    evals = [
        EvalCase(**_eval_case_values_level(fixtures=[FixtureFile(path="app.log", content="E-AUTH-001 login failed")])),
        EvalCase(
            **_eval_case_values_level(id=2, fixtures=[FixtureFile(path="app.log", content="E-DB-002 query error")])
        ),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2  # 不合并:两条独立题
    assert not any(f.startswith("merged:") for e in out.evals for f in (e.flags or []))
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)
    # 各自的 fixture 原样保留(没被 union 混在一起)
    contents = sorted(fx.content for e in out.evals for fx in e.fixtures)
    assert contents == ["E-AUTH-001 login failed", "E-DB-002 query error"]


def test_merge_same_task_evals_split_on_contains_vs_not_contains():
    """Gate 2a:同 prompt、同 fixture(隔离掉 Gate 1),CONTAINS 字面量命中 NOT_CONTAINS 正则 → 矛盾。
    (transcript 出题:同任务既挖到'要含 E-AUTH-001'又挖到'不许含错误码正则'。)
    """
    evals = [
        _conflicting_eval(
            1,
            [
                Assertion(
                    name="has",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": "E-AUTH-001", "path": "report.md"},
                )
            ],
        ),
        _conflicting_eval(
            2,
            [
                Assertion(
                    name="no",
                    type=AssertionType.NOT_CONTAINS,
                    params={"pattern": "E-[A-Z]+-\\d+", "path": "report.md"},
                )
            ],
        ),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)


def test_merge_same_task_evals_split_on_same_key_different_constant():
    """Gate 2b:同 (file,key) 的 CONTAINS_PATTERN 钉成不同常量 → 矛盾。
    (transcript 出题:成功段挖到 total_errors=0、失败段挖到 total_errors=3。)
    """
    evals = [
        _conflicting_eval(
            1,
            [
                Assertion(
                    name="zero",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": '"total_errors": 0', "path": "report.json"},
                )
            ],
        ),
        _conflicting_eval(
            2,
            [
                Assertion(
                    name="three",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": '"total_errors": 3', "path": "report.json"},
                )
            ],
        ),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 2
    assert all(any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals)


def test_merge_does_not_flag_regex_contains_as_conflict():
    """保守:CONTAINS_PATTERN 本身是正则(含元字符)→ 证不了字面重叠,跳过,不误判冲突。
    两条 otherwise 一致的同 prompt eval(一条 CONTAINS 正则、一条 NOT_CONTAINS 不相干)→ 仍合并。
    """
    evals = [
        EvalCase(
            **_eval_case_values_prompt(
                fixtures=[FixtureFile(path="app.log", content="x")],
                assertions=[
                    Assertion(
                        name="re",
                        type=AssertionType.CONTAINS_PATTERN,
                        params={"pattern": "E-\\d+", "path": "report.md"},
                    )
                ],
            )
        ),
        EvalCase(
            **_eval_case_values_prompt(
                id=2,
                fixtures=[FixtureFile(path="app.log", content="x")],
                assertions=[
                    Assertion(
                        name="no", type=AssertionType.NOT_CONTAINS, params={"pattern": "TODO", "path": "report.md"}
                    )
                ],
            )
        ),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 1  # 一致 → 合并
    assert any(f.startswith("merged:") for f in (out.evals[0].flags or []))


def test_merge_partial_conflict_only_splits_conflicting_group():
    """一组同 prompt 冲突(拆)、一组同 prompt 一致(合)、一条不同 prompt(原样)→ 三类各得其所;
    id 全局连续重排(拆出的多条也要各占一个 id)。
    """
    evals = [
        # 组 A:冲突(同 path 异 content)→ 拆成 2 条
        EvalCase(**_eval_case_values_level(prompt="任务一", fixtures=[FixtureFile(path="app.log", content="A1")])),
        EvalCase(
            **_eval_case_values_level(id=2, prompt="任务一", fixtures=[FixtureFile(path="app.log", content="A2")])
        ),
        # 组 B:一致(同 path 同 content)→ 合成 1 条
        EvalCase(**_eval_case_values_level(id=3, prompt="任务二", fixtures=[FixtureFile(path="app.log", content="B")])),
        EvalCase(**_eval_case_values_level(id=4, prompt="任务二", fixtures=[FixtureFile(path="app.log", content="B")])),
        # 组 C:不同 prompt → 原样
        EvalCase(id=5, prompt="任务三", level="L1"),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 4  # 组 A 拆 2 + 组 B 合 1 + 组 C 1
    assert [e.id for e in out.evals] == [1, 2, 3, 4]  # id 连续
    prompts = [e.prompt for e in out.evals]
    assert prompts.count("任务一") == 2  # 拆开的两条都还在
    assert prompts.count("任务二") == 1  # 合并
    a_flags = [any(f.startswith("split:") for f in (e.flags or [])) for e in out.evals if e.prompt == "任务一"]
    assert a_flags == [True, True]
    b = next(e for e in out.evals if e.prompt == "任务二")
    assert any(f.startswith("merged:") for f in (b.flags or []))


def test_enforce_keeps_pathless_contains_pattern():
    ev = EvalCase(
        **_eval_case_values(
            level="L1",
            assertions=[Assertion(name="k", type=AssertionType.CONTAINS_PATTERN, params={"pattern": "keyword"})],
        )
    )  # no path
    out = enforce_manifest(_es(ev), {"sub/cli.py"})
    assert len(out.evals[0].assertions) == 1  # kept, not dropped
    assert out.evals[0].flags == []  # no drop flag


def test_enforce_rewrites_json_has_field_filename():
    ev = EvalCase(
        **_eval_case_values(
            assertions=[
                Assertion(
                    name="f", type=AssertionType.JSON_HAS_FIELD, params={"filename": "config.json", "field": "a.b"}
                )
            ]
        )
    )
    out = enforce_manifest(_es(ev), {"sub/config.json"})
    assert out.evals[0].assertions[0].params["filename"] == "sub/config.json"
    assert out.evals[0].assertions[0].params["field"] == "a.b"  # untouched


def test_enforce_drops_custom_script_resolves_but_no_fixture():
    # script basename resolves to a manifest path, but eval ships no such fixture -> drop
    ev = EvalCase(
        **_eval_case_values_fixtures(
            assertions=[Assertion(name="run", type=AssertionType.CUSTOM_SCRIPT, params={"script": "check.py"})]
        )
    )
    out = enforce_manifest(_es(ev), {"sub/check.py"})  # manifest has it, fixture doesn't
    assert out.evals[0].assertions == []
    assert out.evals[0].flags


def test_generate_from_transcript_missing_file_raises(tmp_path):
    from sift.errors import SiftError

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
    with pytest.raises(SiftError):
        EvalInitGenerator().generate_from_transcript(sp, tmp_path / "nope.jsonl")


# ---------------------------------------------------------------------------
# 方向 B:把 skill 声明的产物纳入 mining manifest
# (原则性客观断言源自 skill、transcript 无关;违规 transcript 不再倒扣)
# ---------------------------------------------------------------------------


class TestExtractSkillOutputs:
    """_extract_skill_outputs:确定性抽 SKILL.md 声明的【产物】文件名,不含输入/参考资料。"""

    @staticmethod
    def test_output_section_filenames_admitted():
        body = "## 输出\n\nerror-report.json 含 total_errors 整数、errors 列表。\n"
        assert extract_skill_outputs(body) == {"error-report.json"}

    @staticmethod
    def test_excludes_references_and_read_only_inputs():
        body = (
            "## 步骤\n\n"
            "1. 读取 references/error-codes.md 做分类。\n"
            "2. 写出 error-report.json。\n\n"
            "## 参考资料\n\n"
            "权威表在 references/error-codes.md;安全规则在 references/security-policy.md。\n\n"
            "## 输出\n\nerror-report.json 含 total_errors。\n"
        )
        out = extract_skill_outputs(body)
        assert "error-report.json" in out
        assert "error-codes.md" not in out
        assert "security-policy.md" not in out
        assert not any("error-codes" in p or "security-policy" in p for p in out)

    @staticmethod
    def test_write_verb_in_steps_admits_without_output_section():
        body = "## 步骤\n\n3. 生成 report.json 保存结果。\n"
        assert "report.json" in extract_skill_outputs(body)

    @staticmethod
    def test_read_verb_in_steps_not_admitted():
        body = "## 步骤\n\n1. 读取 input.csv 做处理。\n"
        assert extract_skill_outputs(body) == set()

    @staticmethod
    def test_no_signal_returns_empty():
        body = "## 规则\n\n禁止编造错误码。\n## 背景\n\n见 error-codes.md。\n"
        assert extract_skill_outputs(body) == set()


def test_from_transcript_keeps_skill_output_assertion_on_violation_transcript(monkeypatch, tmp_path):
    """B 端到端:违规 transcript(只读没写)没产 error-report.json,但 skill '## 输出' 声明它 →
    mining 应保住 FILE_EXISTS error-report.json(改前因 closed-set manifest 把它 drop)。
    原则性断言 transcript 无关、源自 skill。
    """
    import json

    gen = EvalInitGenerator()
    tp = _write_missing_output_transcript(tmp_path)
    sp = _generator_skill(
        tmp_path,
        (
            "---\n"
            "name: log\n"
            "description: d\n"
            "---\n"
            "# 日志分析\n"
            "\n"
            "## 步骤\n"
            "\n"
            "1. 读取 app.log。\n"
            "2. 写出 error-report.json。\n"
            "\n"
            "## 输出\n"
            "\n"
            "error-report.json 含 total_errors。\n"
        ),
    )
    fake = json.dumps(
        {
            "skill_name": "log",
            "evals": [
                _generated_eval(
                    prompt="分析 app.log 写 error-report.json",
                    assertions=[
                        {"name": "report exists", "type": "FILE_EXISTS", "params": {"filename": "error-report.json"}}
                    ],
                )
            ],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    es = gen.generate_from_transcript(sp, tp)
    fns = [a.params.get("filename") for a in es.evals[0].assertions if a.type == AssertionType.FILE_EXISTS]
    assert "error-report.json" in fns  # 保住了(改前被 drop)


def _write_missing_output_transcript(tmp_path):
    import json

    tp = tmp_path / "t.jsonl"
    transcript_records = [
        {"message": {"role": "user", "content": "分析 logs/app.log 写 error-report.json"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/ws/logs/app.log"}}
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "E-AUTH-001 login failed"}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "我不写 error-report.json 了,直接总结。"}],
            }
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    return tp


# ---------------------------------------------------------------------------
# mining --kind skill:多 skill transcript 切分(平行 audit --kind skill)
# ---------------------------------------------------------------------------


def _multi_skill_transcript(tmp_path: Path, target: str = "skillA", other: str = "skillB") -> Path:
    """两 skill 交替的 transcript:target 写 target.json、other 写 other.json。"""
    import json

    tp = tmp_path / "multi.jsonl"
    transcript_records = [
        {"message": {"role": "user", "content": "do A then B"}},
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": target}}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": f"/p/{target}.json", "content": "a"},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "s2", "name": "Skill", "input": {"skill": other}}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w2",
                        "name": "Write",
                        "input": {"file_path": f"/p/{other}.json", "content": "b"},
                    }
                ],
            }
        },
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    return tp


def test_invoked_skills_lists_all_in_order(tmp_path: Path):
    from sift.audit.slice import invoked_skills

    tp = _multi_skill_transcript(tmp_path)
    assert invoked_skills(tp) == ["skillA", "skillB"]


def test_invoked_agents_lists_all_in_order(tmp_path: Path):
    """invoked_agents:record.agent 去重 + 按首次出现顺序;无 agent 字段的 record 跳过。"""
    import json

    from sift.audit.slice import invoked_agents

    tp = tmp_path / "multi-agent.jsonl"
    transcript_records = [
        {"agent": "orchestrator", "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]}},
        {"agent": "reviewer", "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]}},
        {"agent": "orchestrator", "message": {"role": "assistant", "content": [{"type": "text", "text": "c"}]}},
        {"message": {"role": "user", "content": "no-agent-field"}},
    ]
    tp.write_text(
        "\n".join(json.dumps(r) for r in transcript_records),
        encoding="utf-8",
    )
    assert invoked_agents(tp) == ["orchestrator", "reviewer"]


def test_from_transcript_kind_isolates_target_segment(monkeypatch, tmp_path: Path):
    """--kind skill:只挖本 skill 段 → manifest 只含本 skill 文件,别 skill 的断言被 drop。
    对照:不切分(默认)会把别 skill 文件也当本 skill → 混入其他任务内容(两者都留)。
    """
    import json

    gen = EvalInitGenerator()
    sp = _generator_skill(tmp_path, "---\nname: skillA\ndescription: d\n---\n# b\n")
    tp = _multi_skill_transcript(tmp_path)
    fake = json.dumps(
        {
            "skill_name": "skillA",
            "evals": [
                _generated_eval(
                    assertions=[
                        {"name": "A", "type": "FILE_EXISTS", "params": {"filename": "skillA.json"}},
                        {"name": "B", "type": "FILE_EXISTS", "params": {"filename": "skillB.json"}},
                    ]
                )
            ],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    # kind="skill":只切 skillA 段 → skillA.json 留、skillB.json drop
    es = gen.generate_from_transcript(sp, tp, kind="skill")
    fns = [a.params.get("filename") for a in es.evals[0].assertions if a.type == AssertionType.FILE_EXISTS]
    assert "skillA.json" in fns and "skillB.json" not in fns
    # 对照 kind=None:整份 transcript,skillB.json 也在 manifest → 留(串味)
    es2 = gen.generate_from_transcript(sp, tp, kind=None)
    fns2 = [a.params.get("filename") for a in es2.evals[0].assertions if a.type == AssertionType.FILE_EXISTS]
    assert "skillA.json" in fns2 and "skillB.json" in fns2


def test_from_transcript_kind_target_never_invoked_raises(tmp_path: Path):
    """--kind skill 但本 skill 从未在 transcript 被调用 → 明确报错(别静默挖空)。"""
    from sift.errors import SiftError

    gen = EvalInitGenerator()
    sp = _generator_skill(tmp_path, "---\nname: skillC\ndescription: d\n---\n# b\n")
    tp = _multi_skill_transcript(tmp_path)  # 含 skillA + skillB,无 skillC
    with pytest.raises(SiftError):
        gen.generate_from_transcript(sp, tp, kind="skill")


# ---------------------------------------------------------------------------
# 候选①:per-eval rationale + confidence(审阅抓手 + 路 B instruction_ref 种子)
# ---------------------------------------------------------------------------


def test_knowledge_prompt_asks_for_rationale_and_confidence():
    p = build_knowledge_prompt("s", "d", "# body\n")
    assert "rationale" in p and "confidence" in p


def test_procedural_prompt_asks_for_rationale_and_confidence():
    p = build_procedural_prompt("s", "d", "# body\n")
    assert "rationale" in p and "confidence" in p


def test_transcript_prompt_asks_for_rationale():
    digest = SessionDigest(user_turns=["t"], input_files=[], produced_files=[])
    p = build_transcript_prompt(digest=digest, source=PromptSource(name="s", description="d", content="body"))
    assert "rationale" in p


def test_parse_llm_response_populates_rationale_confidence():
    import json

    resp = json.dumps(
        {
            "skill_name": "s",
            "evals": [
                {
                    "id": 1,
                    "name": "n",
                    "level": "L1",
                    "prompt": "p",
                    "rationale": "## 决策 bullet 1",
                    "confidence": 0.9,
                    "expectations": [],
                    "assertions": [],
                    "fixtures": [],
                }
            ],
        }
    )
    es = parse_eval_response(resp).eval_set
    assert es is not None
    assert es.evals[0].rationale == "## 决策 bullet 1"
    assert abs(es.evals[0].confidence - 0.9) < 1e-9


def test_generate_with_llm_llm_failure_flags_degraded(monkeypatch, tmp_path):
    """LLM 调用 RuntimeError → 先重试(_INIT_LLM_MAX_ATTEMPTS 次)再模板兜底 + degraded flag。"""
    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n\n## R\n\nrule one\n")
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("cc timeout")

    monkeypatch.setattr("sift.evals.generator.call_claude", boom)
    es = EvalInitGenerator().generate_with_llm(sp)
    assert calls["n"] == 3, "应重试满 1+2 次"
    assert es.evals, "模板兜底应仍有题"
    for e in es.evals:
        assert any(f.startswith("degraded:llm-fallback") and "重试 2 次后仍失败" in f for f in (e.flags or [])), e.flags


def test_generate_with_llm_unparseable_response_flags_degraded(monkeypatch, tmp_path):
    """LLM 响应非 JSON(解析失败)→ 同样先重试再模板兜底 + degraded flag。"""
    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n\n## R\n\nrule one\n")
    calls = {"n": 0}

    def garbage(*a, **k):
        calls["n"] += 1
        return "sorry I cannot output json"

    monkeypatch.setattr("sift.evals.generator.call_claude", garbage)
    es = EvalInitGenerator().generate_with_llm(sp)
    assert calls["n"] == 3
    assert es.evals
    for e in es.evals:
        flags = e.flags or []
        assert any(f.startswith("degraded:llm-fallback") and "解析失败" in f for f in flags), flags


def test_generate_with_llm_normalizes_absolute_fixture_paths(monkeypatch, tmp_path):
    """LLM 把 fixture 路径写成绝对(/tmp/xxx/…)→ 归一为相对:fixture.path/prompt/
    expectations/断言 params 同步剥前缀,打 review:abs-paths-normalized flag。

    执行器 cwd = run 隔离目录:绝对路径会让 fixture 写穿隔离、grading 却在 run 目录
    查 → 全体假 FAIL,故必须归一。
    """
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n\n## R\n\nrule one\n")
    fake = json.dumps(
        {
            "skill_name": "s",
            "evals": [
                _sourced_eval(
                    name="L2-x",
                    level="L2",
                    prompt=(
                        "Triage /tmp/eval-a/app.log with /tmp/eval-a/references/codes.md, write /tmp/eval-a"
                        "/error-report.json"
                    ),
                    expectations=["reads /tmp/eval-a/app.log first"],
                    assertions=[{"name": "report", "type": "FILE_EXISTS", "params": {"filename": "error-report.json"}}],
                    fixtures=[
                        {"path": "/tmp/eval-a/app.log", "content": "ERR x"},
                        {"path": "/tmp/eval-a/references/codes.md", "content": "ERR_DB_001"},
                    ],
                )
            ],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    es = EvalInitGenerator().generate_with_llm(sp)
    e = es.evals[0]
    assert [f.path for f in e.fixtures] == ["app.log", "references/codes.md"]
    assert "/tmp/eval-a" not in e.prompt
    assert "app.log" in e.prompt and "references/codes.md" in e.prompt
    assert "/tmp/eval-a" not in e.expectations[0]
    # 本就相对的断言 filename 不受影响
    assert e.assertions[0].params["filename"] == "error-report.json"
    assert "review:abs-paths-normalized" in (e.flags or [])


def test_generate_with_llm_relative_paths_untouched(monkeypatch, tmp_path):
    """fixture 本就是相对路径 → 原样返回,不打 flag。"""
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n\n## R\n\nrule one\n")
    fake = json.dumps(
        {
            "skill_name": "s",
            "evals": [
                _sourced_eval(
                    name="L2-x",
                    level="L2",
                    prompt="Triage app.log",
                    expectations=["reads app.log"],
                    fixtures=[{"path": "app.log", "content": "ERR x"}],
                )
            ],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    es = EvalInitGenerator().generate_with_llm(sp)
    e = es.evals[0]
    assert e.fixtures[0].path == "app.log"
    assert "review:abs-paths-normalized" not in (e.flags or [])


def test_generate_with_llm_retry_rescues_transient_failure(monkeypatch, tmp_path):
    """前两次失败(调用错/解析错)第三次成功 → 重试救回,不出 degraded flag。"""
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n\n## R\n\nrule one\n")
    good = json.dumps(
        {
            "skill_name": "s",
            "evals": [_sourced_eval()],
        }
    )
    seq = iter([RuntimeError("529"), "not json", good])

    def flaky(*a, **k):
        v = next(seq)
        if isinstance(v, RuntimeError):
            raise v
        return v

    monkeypatch.setattr("sift.evals.generator.call_claude", flaky)
    beats: list[str] = []
    es = EvalInitGenerator().generate_with_llm(sp, on_heartbeat=beats.append)
    assert es.evals[0].prompt == "p"
    assert not any(f.startswith("degraded:") for f in (es.evals[0].flags or []))
    assert len(beats) == 2, "两次失败各报一条重试心跳"
    assert any("重试 1/2" in b for b in beats)


def test_generate_with_llm_flags_missing_rationale(monkeypatch, tmp_path):
    """LLM 没给 rationale → 标 review:no-rationale(不拒;拒会掉确定性 fallback 更差)。"""
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
    fake = json.dumps(
        {
            "skill_name": "s",
            "evals": [
                {
                    "id": 1,
                    "name": "n",
                    "level": "L1",
                    "prompt": "p",
                    "expectations": ["e"],
                    "assertions": [],
                    "fixtures": [],
                }
            ],
        }
    )  # 无 rationale
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    es = EvalInitGenerator().generate_with_llm(sp)
    assert "review:no-rationale" in (es.evals[0].flags or [])


def test_generate_with_llm_no_flag_when_rationale_present(monkeypatch, tmp_path):
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
    fake = json.dumps(
        {
            "skill_name": "s",
            "evals": [_sourced_eval(rationale="## 规则")],
        }
    )
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *a, **k: fake)
    es = EvalInitGenerator().generate_with_llm(sp)
    assert "review:no-rationale" not in (es.evals[0].flags or [])


def test_collect_unknown_field_warnings_flags_typo():
    """LLM 把 expected_output 写成 expected、assertions 写成 assertion → 抓出来。

    pydantic extra=ignore 会静默丢这些字段,考卷看着生成成功实则缺字段;在 validate
    前扫 raw dict 才能抓到(只 warn 不拒,保住其余合法字段)。
    """
    data = {
        "skill_name": "s",
        "evals": [
            {"id": 1, "prompt": "p", "expected": "x", "assertion": [{"name": "n", "type": "FILE_EXISTS"}]},
        ],
    }
    warns = collect_unknown_field_warnings(data)
    assert len(warns) == 1
    assert "expected" in warns[0]
    assert "assertion" in warns[0]


def test_collect_unknown_field_warnings_clean_when_all_known():
    """全合法字段 → 不报警(已知字段集取自 EvalCase.model_fields,自动随 model 演化)。"""
    data = {
        "skill_name": "s",
        "evals": [
            {
                "id": 1,
                "prompt": "p",
                "expected_output": "x",
                "assertions": [],
                "expectations": [],
                "rationale": "## X",
                "confidence": 0.8,
            },
        ],
    }
    assert collect_unknown_field_warnings(data) == []


def test_collect_unknown_field_warnings_locates_by_index_when_no_id():
    """raw eval 没 id 时用序号定位(让人能找到是哪条)。"""
    data = {
        "skill_name": "s",
        "evals": [
            {"prompt": "p", "expected": "x"},
        ],
    }
    warns = collect_unknown_field_warnings(data)
    assert len(warns) == 1
    assert "expected" in warns[0]


def test_parse_llm_response_records_unknown_field_warnings(tmp_path, monkeypatch):
    """端到端:_parse_llm_response 解析后 gen.init_warnings 含拼错字段(仍解析成功,不拒)。"""
    import json

    gen = EvalInitGenerator()
    resp = json.dumps(
        {
            "skill_name": "s",
            "evals": [
                {
                    "id": 1,
                    "name": "n",
                    "prompt": "p",
                    "expected": "x",  # expected 拼错
                    "expectations": [],
                    "assertions": [],
                    "fixtures": [],
                }
            ],
        }
    )
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: s\ndescription: sample\n---\n# Output\nReturn a result.\n")
    monkeypatch.setattr("sift.evals.generator.call_claude", lambda *args, **kwargs: resp)
    es = gen.generate_with_llm(skill)
    assert es is not None  # extra=ignore → 仍解析成功
    assert any("expected" in w for w in gen.init_warnings)


def test_merge_same_task_evals_preserves_rationale_confidence():
    """合并同 prompt eval:rationale/confidence 取 first(同 prompt 同溯源),不丢。"""
    evals = [
        EvalCase(id=1, prompt="同任务", level="L1", rationale="## X", confidence=0.7, expectations=["e1"]),
        EvalCase(id=2, prompt="同任务", level="L1", rationale="## X", confidence=0.7, expectations=["e2"]),
    ]
    out = merge_same_task_evals(EvalSet(skill_name="s", evals=evals))
    assert len(out.evals) == 1
    assert out.evals[0].rationale == "## X"
    assert abs(out.evals[0].confidence - 0.7) < 1e-9


# ---- --focus:定向补 LLM 生成整套时的盲区(预设类 negative-cases/edge-cases + 自由文本)----


def test_focus_directive_empty_when_no_focus():
    """无 focus → 不注入(默认生成整套,行为不变)。"""
    assert focus_directive(None) == ""
    assert focus_directive("") == ""
    assert focus_directive("   ") == ""


def test_focus_directive_negative_cases_preset():
    """--focus negative-cases → 精调反面场景指令(强制补"skill 不该被触发"盲区)。大小写不敏感。"""
    d = focus_directive("negative-cases")
    assert "## Focus" in d
    assert "NOT" in d  # 强调不该触发/refuse
    assert "## Focus" in focus_directive("Negative-Cases")  # 大小写不敏感


def test_focus_directive_anti_triggers_legacy_alias():
    """旧名 anti-triggers → 仍解析成同一指令(back-compat,避免静默走自由文本回退)。"""
    d_old = focus_directive("anti-triggers")
    d_new = focus_directive("negative-cases")
    assert d_old == d_new
    assert "## Focus" in d_old and "NOT" in d_old


def test_focus_directive_edge_cases_preset():
    """--focus edge-cases → 精调边界/错误处理指令(强制补边界盲区)。"""
    d = focus_directive("edge-cases")
    assert "## Focus" in d
    assert "error" in d.lower() or "boundary" in d.lower() or "edge" in d.lower()


def test_focus_directive_free_text_fallback():
    """--focus '加密 PDF'(非预设类名)→ 自由文本注入(skill 特定方向)。"""
    d = focus_directive("加密 PDF")
    assert "## Focus" in d
    assert "加密 PDF" in d  # 用户原话进 prompt


def test_build_llm_prompt_injects_focus_directive():
    """--focus 注入到 prompt(## Output JSON 前),改变这次生成目标。"""
    prompt = build_llm_prompt(
        is_procedural=False, focus="negative-cases", source=PromptSource(name="s", description="d", content="# b\n")
    )
    assert "## Focus" in prompt
    assert prompt.index("## Focus") < prompt.index("## Output JSON")  # 注入在 Output JSON 前


def test_build_llm_prompt_without_focus_unchanged():
    """无 focus → prompt 不含 ## Focus(默认整套,行为不变)。"""
    prompt = build_llm_prompt(is_procedural=False, source=PromptSource(name="s", description="d", content="# b\n"))
    assert "## Focus" not in prompt


# ---------------------------------------------------------------------------
# trigger-accuracy 探题生成(Task 6):generate_trigger_probes
# 读 description 生成 should/should-not trigger 探题,prompt 强制 neutral wording。
# ---------------------------------------------------------------------------


def _skill(tmp_path):
    s = tmp_path / "my"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: my\ndescription: does X\n---\n# body\n")
    return s


def test_generate_trigger_probes_parses_llm_json(tmp_path):
    fake = (
        '{"skill_name":"my","trigger_probes":['
        '{"id":1,"prompt":"做这个 skill 该做的事","expect_trigger":true,"rationale":"正中用途"},'
        '{"id":2,"prompt":"看着像但不该管的事","expect_trigger":false,"rationale":"无关"}]}'
    )
    gen = EvalInitGenerator()
    with patch("sift.evals.generator.call_claude", return_value=fake):
        probes = gen.generate_trigger_probes(_skill(tmp_path))
    assert len(probes) == 2
    assert probes[0].expect_trigger is True and probes[1].expect_trigger is False
    assert probes[0].rationale  # 带溯源


def test_trigger_probe_prompt_is_neutral_and_does_not_name_skill(tmp_path):
    """生成 prompt 必须强制 neutral wording、不命名 skill(否则不是测触发)。"""
    gen = EvalInitGenerator()
    with patch("sift.evals.generator.call_claude") as mc:
        mc.return_value = '{"skill_name":"my","trigger_probes":[]}'
        gen.generate_trigger_probes(_skill(tmp_path))
        sent = mc.call_args.kwargs["prompt"]
    assert "neutral" in sent.lower()
    assert "do not name" in sent.lower()
    assert "paraphrase" in sent.lower()  # anti-leakage:不许照抄 description 措辞


def test_generate_trigger_probes_extracts_json_embedded_in_prose(tmp_path):
    """LLM 把 JSON 包在散文里 → 正则回退抽出。"""
    fake = (
        'Here you go:\n{"skill_name":"my","trigger_probes":'
        '[{"id":1,"prompt":"q","expect_trigger":true,"rationale":"r"}]}\nDone.'
    )
    gen = EvalInitGenerator()
    with patch("sift.evals.generator.call_claude", return_value=fake):
        probes = gen.generate_trigger_probes(_skill(tmp_path))
    assert len(probes) == 1 and probes[0].expect_trigger is True


def test_generate_trigger_probes_empty_on_llm_failure(tmp_path):
    """call_claude 抛异常 → 返 [](under-delivery 由 init 层处理)。"""
    gen = EvalInitGenerator()

    def boom(*a, **k):
        raise RuntimeError("llm down")

    with patch("sift.evals.generator.call_claude", side_effect=boom):
        probes = gen.generate_trigger_probes(_skill(tmp_path))
    assert probes == []


def test_generate_trigger_probes_non_dict_json_returns_empty(tmp_path):
    """响应是合法 JSON 但非对象(裸数组)→ 返 [],不 AttributeError 炸穿 init。"""
    gen = EvalInitGenerator()
    with patch("sift.evals.generator.call_claude", return_value='["probe", "probe2"]'):
        probes = gen.generate_trigger_probes(_skill(tmp_path))
    assert probes == []


def test_generate_trigger_probes_non_list_probes_returns_empty(tmp_path):
    """trigger_probes 键在但值非数组(裸 int/bool)→ 返 [],不 TypeError 炸穿 init。

    enumerate 取迭代器这步就抛,循环体内的逐条 try 救不到。
    """
    gen = EvalInitGenerator()
    with patch("sift.evals.generator.call_claude", return_value='{"trigger_probes": 5}'):
        probes = gen.generate_trigger_probes(_skill(tmp_path))
    assert probes == []


# ---------------------------------------------------------------------------
# 共享内核:定向补题(_supplement_directive)
# 有现有题(init --append / coverage --augment)→ 硬化 focus + 喂现有题清单做语义去重;
# 治「半聚焦 + 不去重」(旧 _focus_directive 软提示,LLM 端到端生成、与现有题重复)。
# ---------------------------------------------------------------------------


def test_supplement_directive_falls_back_to_focus_when_no_existing():
    """无现有题(覆盖式 init)→ 退回软 _focus_directive,行为不变。"""
    assert supplement_directive("加密 PDF", None) == focus_directive("加密 PDF")
    assert supplement_directive("加密 PDF", []) == focus_directive("加密 PDF")
    assert supplement_directive(None, None) == ""


def test_supplement_directive_lists_existing_evals_for_dedup():
    """有现有题 → 注入清单(name/prompt/rationale)让 LLM 语义避重(不只字面)。"""
    existing = [
        EvalCase(id=2, name="empty_log", prompt="分析空日志", level="L1", rationale="## total-errors-count 空日志→0"),
    ]
    d = supplement_directive("read-log-file", existing)
    assert "empty_log" in d and "分析空日志" in d and "total-errors-count" in d


def test_supplement_directive_strong_constraint_and_focus():
    """有现有题 → 硬约束(非整套/不重复/assertion 验证 gap)+ focus 注入。"""
    d = supplement_directive("read-log-file", [EvalCase(id=1, prompt="x")])
    assert "TARGETED SUPPLEMENT" in d
    assert "NOT a fresh full suite" in d
    assert "duplicate" in d.lower()
    assert "read-log-file" in d


def test_build_agent_prompt_uses_supplement_with_existing():
    """_build_agent_prompt 接 _supplement_directive:有 existing + focus → prompt 含
    硬约束补 gap 指令(TARGETED SUPPLEMENT / 不重复 / 现有题 rationale),不再是旧的软
    提示「别重复,出新角度」。与 skill _build_llm_prompt 对称(补齐 agent 路径缺口)。
    """
    existing = [EvalCase(id=1, name="normal-diff", prompt="审常规 diff", level="L1", rationale="考常规 PR 审查")]
    prompt = build_agent_prompt(
        focus="negative-cases",
        existing_evals=existing,
        source=PromptSource(
            name="reviewer", description="审 PR", content="# 审查\n只读", frontmatter={"tools": ["Read", "Grep"]}
        ),
    )
    assert "TARGETED SUPPLEMENT" in prompt  # 走 _supplement_directive(硬约束)
    assert "duplicate" in prompt.lower()  # 去重硬约束
    assert "考常规 PR 审查" in prompt  # 现有题 rationale 喂进 prompt(语义去重证据)
    assert "别重复,出新角度" not in prompt  # 旧软提示已撤


def test_build_agent_prompt_focus_only_is_soft():
    """无 existing(首次出题)+ focus → 软 focus directive,不走硬 supplement 约束。"""
    prompt = build_agent_prompt(
        focus="加密 PDF",
        existing_evals=None,
        source=PromptSource(name="reviewer", description="审 PR", content="# 审查\n只读", frontmatter={"tools": []}),
    )
    assert "加密 PDF" in prompt
    assert "TARGETED SUPPLEMENT" not in prompt  # 无 existing 不走硬约束


def test_build_llm_prompt_supplement_when_existing_evals():
    """_build_llm_prompt(existing_evals=...) → 注入补题指令(含现有题),在 Output JSON 前。"""
    p = build_llm_prompt(
        is_procedural=False,
        focus="read-log-file",
        existing_evals=[EvalCase(id=1, name="old", prompt="p")],
        source=PromptSource(name="s", description="d", content="# b\n"),
    )
    assert "TARGETED SUPPLEMENT" in p
    assert "old" in p
    assert p.index("TARGETED SUPPLEMENT") < p.index("## Output JSON")


def test_build_llm_prompt_no_supplement_without_existing():
    """无 existing + focus → 仍是软 _focus_directive(不硬化,覆盖式 init 行为不变)。"""
    p = build_llm_prompt(
        is_procedural=False, focus="negative-cases", source=PromptSource(name="s", description="d", content="# b\n")
    )
    assert "TARGETED SUPPLEMENT" not in p
    assert "## Focus" in p


def test_generate_with_llm_passes_existing_into_prompt(monkeypatch, tmp_path):
    """generate_with_llm(existing_evals=[...]) → 喂进 call_claude 的 prompt(含现有题 + 硬约束)。"""
    import json

    sp = _generator_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
    captured = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "skill_name": "s",
                "evals": [_sourced_eval(rationale="## r", expectations=[])],
            }
        )

    monkeypatch.setattr("sift.evals.generator.call_claude", fake)
    EvalInitGenerator(focus="read-log-file").generate_with_llm(
        sp, existing_evals=[EvalCase(id=7, name="SENTRY", prompt="old-prompt")]
    )
    assert "SENTRY" in captured["prompt"]
    assert "TARGETED SUPPLEMENT" in captured["prompt"]


def _eval_case_values(**changes):
    data = {"id": 1, "prompt": "x", "level": "L2", "assertions": None}
    data.update(changes)
    return data


def _eval_case_values_fixtures(**changes):
    data = {"id": 1, "prompt": "x", "level": "L2", "assertions": None, "fixtures": []}
    data.update(changes)
    return data


def _eval_case_values_assertions(**changes):
    data = {
        "id": 1,
        "prompt": "分析 app.log 写 report",
        "level": "L1",
        "expected_output": "含 total_errors",
        "expectations": ["要 total_errors"],
        "assertions": [],
        "fixtures": None,
    }
    data.update(changes)
    return data


def _eval_case_values_level(**changes):
    data = {"id": 1, "prompt": "分析 app.log 写报告", "level": "L2", "fixtures": None}
    data.update(changes)
    return data


def _eval_case_values_prompt(**changes):
    data = {"id": 1, "prompt": "分析 app.log 写报告", "level": "L2", "fixtures": None, "assertions": None}
    data.update(changes)
    return data


def _generated_eval(**changes):
    data = {
        "id": 1,
        "name": "L2",
        "level": "L2",
        "prompt": "p",
        "expected_output": "",
        "expectations": [],
        "assertions": [{"name": "cli", "type": "FILE_EXISTS", "params": {"filename": "cli.py"}}],
        "fixtures": [],
    }
    data.update(changes)
    return data


def _sourced_eval(**changes):
    data = {
        "id": 1,
        "name": "n",
        "level": "L1",
        "prompt": "p",
        "rationale": "## R",
        "expectations": ["e"],
        "assertions": [],
        "fixtures": [],
    }
    data.update(changes)
    return data


def _cli_write_records():
    return [
        {"message": {"role": "user", "content": "造 cli"}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "w1",
                        "name": "Write",
                        "input": {"file_path": "/proj/cli.py", "content": "x"},
                    }
                ],
            }
        },
    ]


def _generator_skill(tmp_path, content):
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(content, encoding="utf-8")
    return sp


def _conflicting_eval(eval_id, assertions):
    return EvalCase(
        **_eval_case_values_prompt(
            id=eval_id,
            fixtures=[FixtureFile(path="app.log", content="x")],
            assertions=assertions,
        )
    )
