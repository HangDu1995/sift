"""agent 出题(generate_for_agent)单测。

核:复用 _llm_call/_parse_llm_response/_auto_generate_assertions;prompt 喂 description+body;
LLM 失败 → 空 EvalSet(agent 无确定性 fallback,generate_template 切段是 skill 专属)。
"""
import json

import skill_eval.evals.generator as gen_mod
from skill_eval.evals.generator import EvalInitGenerator


_AGENT_MD = ("---\nname: reviewer\ndescription: 审查 PR diff 给可执行修改建议\n"
             "tools:\n  - Read\n  - Grep\n---\n# 你是审查者\n只读不改,给行级建议\n")


def _fake_agent_evals():
    # 必须含 skill_name —— _parse_llm_response 走 EvalSet.model_validate,
    # skill_name 是必填无默认,缺了 → ValidationError 被吞 → 返 None → 空 EvalSet(假绿陷阱)。
    return json.dumps({
        "skill_name": "reviewer",
        "evals": [
            {"id": 1, "name": "review-diff", "level": "L1",
             "prompt": "审查这个 PR 的 diff", "expected_output": "行级可执行修改建议",
             "expectations": ["指出“具体行”的问题", "不改代码,只给建议", "不应泛泛而谈"],
             "rationale": "审查者核心职责"},
        ]})


def test_generate_for_agent_feeds_description_and_body(monkeypatch, tmp_path):
    """generate_for_agent 把 description + body 喂给 LLM,解析出 EvalSet,断言自动派生。"""
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(_AGENT_MD, encoding="utf-8")
    captured = {}

    def fake_call_claude(prompt, **kw):
        captured["prompt"] = prompt
        return _fake_agent_evals()

    monkeypatch.setattr(gen_mod, "call_claude", fake_call_claude)
    gen = EvalInitGenerator()
    eset = gen.generate_for_agent(agent_md)

    assert eset.skill_name == "reviewer"
    assert len(eset.evals) == 1
    ev = eset.evals[0]
    assert ev.prompt == "审查这个 PR 的 diff"
    assert "审查 PR diff" in captured["prompt"]          # description 喂进去了
    assert "只读不改" in captured["prompt"]               # body 喂进去了
    assert '"skill_name": "reviewer"' in captured["prompt"]   # prompt 示例含 skill_name(防 parse 假绿)
    # L1 断言由 expectations 自动派生:「具体行」→ CONTAINS_PATTERN;不应泛泛→ NOT_CONTAINS
    types = {a.type.value for a in ev.assertions}
    assert "CONTAINS_PATTERN" in types


def test_generate_for_agent_llm_failure_returns_empty(monkeypatch, tmp_path):
    """LLM 失败(agent 无确定性 fallback)→ 空 EvalSet,不崩。"""
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(_AGENT_MD, encoding="utf-8")
    monkeypatch.setattr(gen_mod, "call_claude",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    eset = EvalInitGenerator().generate_for_agent(agent_md)
    assert eset.evals == []
    assert eset.skill_name == "reviewer"


def test_generate_for_agent_parse_failure_returns_empty(monkeypatch, tmp_path):
    """LLM 返回非 JSON(解析失败)→ 空 EvalSet,不崩(不 fallback 到 skill 切段)。"""
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(_AGENT_MD, encoding="utf-8")
    monkeypatch.setattr(gen_mod, "call_claude", lambda *a, **kw: "not json at all")
    eset = EvalInitGenerator().generate_for_agent(agent_md)
    assert eset.evals == []
