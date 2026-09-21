"""trigger 规则建议(build_rule_suggestions)单测。"""

from sift.execution.trigger_suggest import build_rule_suggestions


def _probe(pid, expect_trigger, verdict, prompt="q"):
    return {"id": pid, "expect_trigger": expect_trigger, "prompt": prompt, "verdict": verdict}


def test_recall_fail_lists_prompt_and_ids():
    suggestions = build_rule_suggestions(
        [
            _probe(1, True, "FAIL", "分析这份日志"),
            _probe(2, True, "PASS"),
        ]
    )
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "recall" and s.source == "rule"
    assert s.probe_ids == [1]
    assert "分析这份日志" in s.message and "漏召" in s.message


def test_false_trigger_fail_lists_prompt_and_ids():
    suggestions = build_rule_suggestions(
        [
            _probe(1, False, "FAIL", "翻译这本小说"),
            _probe(2, False, "PASS"),
        ]
    )
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
    suggestions = build_rule_suggestions(
        [
            _probe(1, True, "PASS"),
            _probe(2, False, "PASS"),
        ]
    )
    assert len(suggestions) == 1
    assert suggestions[0].category == "all_good"


def test_mixed_failures_emit_three_suggestions_no_all_good():
    """recall + false_trigger + unresolved 同时存在 → 3 条,不出 all_good。"""
    suggestions = build_rule_suggestions(
        [
            _probe(1, True, "FAIL", "该触发没触发"),
            _probe(2, False, "FAIL", "不该触发却触发"),
            _probe(3, True, "UNRESOLVED"),
        ]
    )
    cats = [s.category for s in suggestions]
    assert cats == ["recall", "false_trigger", "unresolved"]
    assert "all_good" not in cats
