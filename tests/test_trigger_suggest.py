"""trigger 规则建议(build_rule_suggestions)单测。"""

from skill_eval.execution.trigger_suggest import build_rule_suggestions


def _probe(pid, expect_trigger, verdict, prompt="q"):
    return {"id": pid, "expect_trigger": expect_trigger, "prompt": prompt, "verdict": verdict}


def test_recall_fail_lists_prompt_and_ids():
    suggestions = build_rule_suggestions([
        _probe(1, True, "FAIL", "分析这份日志"),
        _probe(2, True, "PASS"),
    ])
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "recall" and s.source == "rule"
    assert s.probe_ids == [1]
    assert "分析这份日志" in s.message and "漏召" in s.message


def test_false_trigger_fail_lists_prompt_and_ids():
    suggestions = build_rule_suggestions([
        _probe(1, False, "FAIL", "翻译这本小说"),
        _probe(2, False, "PASS"),
    ])
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "false_trigger"
    assert s.probe_ids == [1]
    assert "翻译这本小说" in s.message and "误触" in s.message


def test_unresolved_suggestion():
    suggestions = build_rule_suggestions([_probe(1, True, "UNRESOLVED")])
    assert len(suggestions) == 1
    assert suggestions[0].category == "unresolved"
    assert "--runs" in suggestions[0].message


def test_all_good_when_no_failures():
    suggestions = build_rule_suggestions([
        _probe(1, True, "PASS"), _probe(2, False, "PASS"),
    ])
    assert len(suggestions) == 1
    assert suggestions[0].category == "all_good"


def test_mixed_failures_emit_three_suggestions_no_all_good():
    """recall + false_trigger + unresolved 同时存在 → 3 条,不出 all_good。"""
    suggestions = build_rule_suggestions([
        _probe(1, True, "FAIL", "该触发没触发"),
        _probe(2, False, "FAIL", "不该触发却触发"),
        _probe(3, True, "UNRESOLVED"),
    ])
    cats = [s.category for s in suggestions]
    assert cats == ["recall", "false_trigger", "unresolved"]
    assert "all_good" not in cats


# ---------------------------------------------------------------------------
# LLM 建议(build_llm_suggestion:有失败才调、全 PASS 跳过、降级)
# ---------------------------------------------------------------------------

from skill_eval.execution.trigger_suggest import build_llm_suggestion  # noqa: E402


def test_llm_suggestion_called_when_failures(monkeypatch):
    """有失败 probe → 调一次 LLM → 返 llm_rewrite suggestion + usage(token 计入用)。"""
    captured = {}

    def fake(prompt, *, model=None, timeout=120, max_turns=None, on_usage=None):
        captured["prompt"] = prompt
        if on_usage:
            on_usage({"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.01})
        return "修订 description:做日志分析、不翻译\n理由:收窄了 scope"

    monkeypatch.setattr("skill_eval.execution.trigger_suggest.call_claude", fake)
    sugg, usage = build_llm_suggestion(
        "当前 desc", [{"id": 1, "prompt": "分析日志"}, {"id": 3, "prompt": "查异常"}],
        [{"id": 2, "prompt": "翻译小说"}], model="m")
    assert sugg is not None
    assert sugg.category == "llm_rewrite" and sugg.source == "llm"
    assert sugg.probe_ids == [1, 3, 2]                       # 失败 probe 全列
    assert "修订 description" in sugg.message
    assert "当前 desc" in captured["prompt"]                 # 当前 description 喂进去了
    assert usage["input_tokens"] == 100 and usage["total_cost_usd"] == 0.01


def test_llm_suggestion_skipped_when_all_pass(monkeypatch):
    """全 PASS(无失败)→ 不调 call_claude(零成本)。"""
    calls = {"n": 0}

    def fake(prompt, **kw):
        calls["n"] += 1
        return "x"

    monkeypatch.setattr("skill_eval.execution.trigger_suggest.call_claude", fake)
    sugg, usage = build_llm_suggestion("desc", [], [])       # 无失败
    assert sugg is None and usage == {}
    assert calls["n"] == 0                                   # 没调


def test_llm_suggestion_degrades_on_exception(monkeypatch):
    """call_claude 抛异常 → (None, {}),不炸(规则建议照在)。"""

    def fake(prompt, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("skill_eval.execution.trigger_suggest.call_claude", fake)
    sugg, usage = build_llm_suggestion("desc", [{"id": 1, "prompt": "p"}], [])
    assert sugg is None and usage == {}


def test_llm_suggestion_degrades_on_empty_response(monkeypatch):
    """LLM 返空 → (None, {})。"""

    def fake(prompt, *, on_usage=None, **kw):
        if on_usage:
            on_usage({"input_tokens": 1})
        return "   "                                          # 空白

    monkeypatch.setattr("skill_eval.execution.trigger_suggest.call_claude", fake)
    sugg, usage = build_llm_suggestion("desc", [{"id": 1, "prompt": "p"}], [])
    assert sugg is None

