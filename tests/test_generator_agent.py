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
             "expectations": ["指出具体行的问题", "不改代码,只给建议", "不应泛泛而谈"],
             "assertions": [
                 {"name": "mentions 具体行", "type": "CONTAINS_PATTERN", "params": {"pattern": "具体行"}},
                 {"name": "avoids 泛泛", "type": "NOT_CONTAINS", "params": {"pattern": "泛泛"}}
             ],
             "rationale": "审查者核心职责"},
        ]})


def test_generate_for_agent_feeds_description_and_body(monkeypatch, tmp_path):
    """generate_for_agent 把 description + body 喂给 LLM,解析出 EvalSet;LLM 直接写的 assertions 被保留(对齐 skill)。"""
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
    # LLM 直接写的 assertions 被保留(对齐 skill;不再靠 expectations 引号词派生)
    types = {a.type.value for a in ev.assertions}
    assert "CONTAINS_PATTERN" in types
    assert "NOT_CONTAINS" in types
    assert any(a.name == "mentions 具体行" for a in ev.assertions)   # LLM 写的语义化名保留


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


def test_agent_prompt_asks_llm_to_write_assertions_with_natural_expectations():
    """_build_agent_prompt 对齐 skill:expectations 自然语言 concrete claim + 让 LLM 直接写 assertions。

    旧的“引号词 expectations + 系统派生断言”模式撤掉——expectations 改自然语言(可读),
    assertions 由 LLM 直接写(逐条对应、不靠引号词派生丢短词)。
    """
    gen = EvalInitGenerator()
    prompt = gen._build_agent_prompt(
        "reviewer", "审查 PR diff 给可执行修改建议",
        "# 你是审查者\n只读不改", {"tools": ["Read"]})
    # expectations 是自然语言 concrete claim(不再是引号词格式)
    assert ("可观测" in prompt) or ("concrete" in prompt.lower())
    assert "双引号括起来" not in prompt               # 旧引号词要求已撤
    # 模板含 assertions 字段——让 LLM 直接写
    assert '"assertions"' in prompt
    assert "CONTAINS_PATTERN" in prompt
    # Rules 要求 LLM 写 assertions(不再是“系统自动生成”)
    assert "不用自己写 assertions" not in prompt
    assert ("写对应 assertion" in prompt) or ("写 assertions" in prompt)


def test_generate_for_agent_warns_on_thin_assertions(monkeypatch, tmp_path):
    """断言数<期望数(_auto_generate 补不上的自然语言 expectations)→ init warning 提示审。"""
    agent_md = tmp_path / "reviewer.md"
    agent_md.write_text(_AGENT_MD, encoding="utf-8")
    thin = json.dumps({
        "skill_name": "reviewer",
        "evals": [{
            "id": 1, "name": "review-diff", "level": "L1",
            "prompt": "审查 diff", "expected_output": "建议",
            # 自然语言 expectations(无引号词、无「不应X」结构)→ _auto_generate 抽不到
            "expectations": ["指出具体行号的问题", "给改的方向", "解释清晰可执行"],
            "assertions": [  # 1 条 < 3 expectations
                {"name": "mentions 行号", "type": "CONTAINS_PATTERN", "params": {"pattern": "行号"}}
            ],
            "rationale": "审查职责"}]})
    monkeypatch.setattr(gen_mod, "call_claude", lambda *a, **kw: thin)
    gen = EvalInitGenerator()
    gen.generate_for_agent(agent_md)
    assert any("断言" in w and "<" in w for w in gen.init_warnings), gen.init_warnings
