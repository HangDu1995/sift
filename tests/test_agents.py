"""Tests for blind comparator and post-hoc analyzer agents."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skill_eval.models import (
    AnalysisResult,
    ComparisonResult,
    ComparisonOutputQuality,
    ComparisonOutputRubric,
    ComparisonRubricScores,
    ComparisonStructureScores,
)
from skill_eval.scoring.comparator import BlindComparator
from skill_eval.scoring.analyzer import PosthocAnalyzer
from skill_eval.concurrency import _LLMRetryExhausted


VALID_COMPARISON_JSON = json.dumps({
    "winner": "A",
    "reasoning": "Output A is more complete.",
    "rubric": {
        "A": {
            "content": {"correctness": 5, "completeness": 5, "accuracy": 4},
            "structure": {"organization": 4, "formatting": 5, "usability": 4},
            "content_score": 4.7,
            "structure_score": 4.3,
            "overall_score": 9.0,
        },
        "B": {
            "content": {"correctness": 3, "completeness": 2, "accuracy": 3},
            "structure": {"organization": 3, "formatting": 2, "usability": 3},
            "content_score": 2.7,
            "structure_score": 2.7,
            "overall_score": 5.4,
        },
    },
    "output_quality": {
        "A": {"score": 9, "strengths": ["Complete"], "weaknesses": []},
        "B": {"score": 5, "strengths": ["Readable"], "weaknesses": ["Missing fields"]},
    },
})

VALID_COMPARISON_WITH_EXPECTATIONS = json.dumps({
    "winner": "A",
    "reasoning": "A passes more expectations.",
    "rubric": {},
    "output_quality": {},
    "expectation_results": {
        "A": {"passed": 4, "total": 5, "pass_rate": 0.8, "details": [
            {"text": "has name", "passed": True},
            {"text": "has steps", "passed": True},
            {"text": "has examples", "passed": True},
            {"text": "has anti-fabrication", "passed": True},
            {"text": "has references", "passed": False},
        ]},
        "B": {"passed": 3, "total": 5, "pass_rate": 0.6, "details": [
            {"text": "has name", "passed": True},
            {"text": "has steps", "passed": True},
            {"text": "has examples", "passed": True},
            {"text": "has anti-fabrication", "passed": False},
            {"text": "has references", "passed": False},
        ]},
    },
})

VALID_ANALYSIS_JSON = json.dumps({
    "winner_strengths": ["Clear instructions", "Good error handling"],
    "loser_weaknesses": ["Vague instructions", "No validation"],
    "instruction_following": {
        "winner": {"score": 9, "issues": []},
        "loser": {"score": 6, "issues": ["Skipped step 3"]},
    },
    "improvement_suggestions": [
        {"priority": "high", "category": "instructions", "suggestion": "Add explicit steps", "expected_impact": "Would reduce ambiguity"},
    ],
    "transcript_insights": {
        "winner_execution_pattern": "Followed 5 steps",
        "loser_execution_pattern": "Tried 3 methods",
    },
})


class TestComparisonResultModel:
    def test_basic_validation(self):
        r = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_JSON))
        assert r.winner == "A"
        assert "A" in r.rubric
        assert r.rubric["A"].overall_score == 9.0
        assert r.output_quality["A"].score == 9

    def test_with_expectations(self):
        r = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_WITH_EXPECTATIONS))
        assert r.expectation_results is not None
        assert r.expectation_results["A"].pass_rate == 0.8

    def test_minimal(self):
        r = ComparisonResult(winner="TIE", reasoning="Equal outputs")
        assert r.winner == "TIE"
        assert r.rubric == {}
        assert r.pos_a_label == ""   # 未揭盲(没过 _translate)→ 空,viewer 不显标注

    def test_default_scores(self):
        r = ComparisonRubricScores()
        assert r.correctness == 0
        s = ComparisonStructureScores()
        assert s.organization == 0


class TestAnalysisResultModel:
    def test_basic_validation(self):
        r = AnalysisResult.model_validate(json.loads(VALID_ANALYSIS_JSON))
        assert len(r.winner_strengths) == 2
        assert r.instruction_following["winner"].score == 9
        assert len(r.improvement_suggestions) == 1
        assert r.improvement_suggestions[0].priority == "high"

    def test_empty(self):
        r = AnalysisResult()
        assert r.winner_strengths == []
        assert r.improvement_suggestions == []


class _FixedRng:
    """测试用确定性 rng:random() < 0.5 → with 放 A 位(with_is_a=True)。"""

    def __init__(self, with_is_a: bool):
        self._with_is_a = with_is_a

    def random(self) -> float:
        return 0.0 if self._with_is_a else 0.99


class TestBlindComparator:
    def test_loads_prompt_template(self):
        c = BlindComparator()
        assert c._prompt_template != ""
        assert "Blind Comparator" in c._prompt_template

    def test_build_prompt(self):
        c = BlindComparator()
        prompt = c._build_prompt("output A text", "output B text", "Do the task", ["expect 1"])
        assert "output A text" in prompt
        assert "output B text" in prompt
        assert "Do the task" in prompt
        assert "expect 1" in prompt

    def test_build_prompt_no_expectations(self):
        c = BlindComparator()
        prompt = c._build_prompt("A", "B", "task", None)
        assert "--- EXPECTATIONS ---" not in prompt

    def test_parse_valid_json(self):
        c = BlindComparator()
        result = c._parse(VALID_COMPARISON_JSON)
        assert result.winner == "A"
        assert result.rubric["A"].overall_score == 9.0

    def test_parse_json_in_text(self):
        c = BlindComparator()
        text = f"Here is the comparison:\n{VALID_COMPARISON_JSON}\nDone."
        result = c._parse(text)
        assert result.winner == "A"

    def test_parse_empty_response(self):
        c = BlindComparator()
        result = c._parse("")
        assert result.winner == "TIE"
        assert "fail" in result.reasoning.lower() or "失败" in result.reasoning

    def test_parse_invalid_json(self):
        c = BlindComparator()
        result = c._parse("not json at all")
        assert result.winner == "TIE"

    # ── position 随机化 + 语义翻译(梯队一③)──
    # comparator 内部随机决定 with 放 A/B 位(消 LLM position bias),_parse 仍返回
    # raw A/B/TIE,compare() 经 _translate 翻成语义 with/without/tie + dict key 重映射。

    def test_translate_with_is_a_true(self):
        c = BlindComparator()
        raw = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_JSON))  # winner A
        out = c._translate(raw, side0_is_posA=True)
        # side0 在 A 位,LLM 判 A 胜 → 胜者翻成 label_a(default with)
        assert out.winner == "with"
        assert out.rubric["with"].overall_score == 9.0
        assert out.rubric["without"].overall_score == 5.4
        assert out.output_quality["with"].score == 9
        assert out.output_quality["without"].score == 5
        assert out.pos_a_label == "with"       # 揭盲:LLM position A = with
        assert out.pos_b_label == "without"

    def test_translate_with_is_a_false(self):
        c = BlindComparator()
        raw = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_JSON))  # winner A
        out = c._translate(raw, side0_is_posA=False)
        # side0 在 B 位,LLM 判 A 胜 → A 是 side1=without → 胜者翻成 without
        assert out.winner == "without"
        assert out.rubric["without"].overall_score == 9.0
        assert out.rubric["with"].overall_score == 5.4
        assert out.pos_a_label == "without"   # 揭盲:side0 在 B 位 → LLM position A = without
        assert out.pos_b_label == "with"

    def test_translate_v1v2_labels_carried_through(self):
        """v1/v2 双 skill 模式:label_a/b 传 v1/v2(非默认 with/without),揭盲映射也得是 v1/v2、
        不能写死 with/without。pos_a_label/pos_b_label 跟随实际传入的 label。"""
        c = BlindComparator()
        raw = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_JSON))  # winner A
        out = c._translate(raw, side0_is_posA=True, label_a="v1", label_b="v2")
        assert out.winner == "v1"
        assert out.pos_a_label == "v1"        # v1 在 A 位 → 揭盲 A=v1
        assert out.pos_b_label == "v2"
        # side0 在 B 位 → A 位是 side1=v2
        out2 = c._translate(raw, side0_is_posA=False, label_a="v1", label_b="v2")
        assert out2.winner == "v2"
        assert out2.pos_a_label == "v2"
        assert out2.pos_b_label == "v1"

    def test_translate_tie_and_expectation_remap(self):
        c = BlindComparator()
        raw = ComparisonResult.model_validate(json.loads(VALID_COMPARISON_WITH_EXPECTATIONS))
        out = c._translate(raw, side0_is_posA=True)
        assert out.winner == "with"
        assert out.expectation_results["with"].pass_rate == 0.8
        assert out.expectation_results["without"].pass_rate == 0.6
        # tie 与 side0_is_posA 无关
        raw_tie = ComparisonResult(winner="TIE", reasoning="eq")
        assert c._translate(raw_tie, side0_is_posA=True).winner == "tie"
        assert c._translate(raw_tie, side0_is_posA=False).winner == "tie"

    def test_translate_recomputes_expectation_counts_from_details(self):
        """LLM 声明的 passed/total 常数不对自己的 details 列表(一次性产 JSON,同 rubric
        4/5/4→4.67 那类自报不精确)。details 是逐条 ground truth——_translate 据此重算汇总数
        覆盖声明值,否则 viewer 顶部计数与下方逐条 ✓/✗ 对不上。"""
        c = BlindComparator()
        details = ([{"text": f"exp-{i}", "passed": True} for i in range(7)]
                   + [{"text": f"exp-{i}", "passed": False} for i in range(4)])  # 7 ✓ + 4 ✗
        raw = ComparisonResult.model_validate({
            "winner": "A", "reasoning": "A better", "rubric": {}, "output_quality": {},
            "expectation_results": {
                "A": {"passed": 9, "total": 11, "pass_rate": 0.82, "details": details},
                "B": {"passed": 5, "total": 11, "pass_rate": 0.45, "details": details},
            },
        })
        out = c._translate(raw, side0_is_posA=True)
        er_a = out.expectation_results["with"]
        assert er_a.passed == 7          # 不是 LLM 声明的 9
        assert er_a.total == 11
        assert er_a.pass_rate == 7 / 11
        er_b = out.expectation_results["without"]
        assert er_b.passed == 7          # B 套同一 details 也重算成 7(非声明的 5)

    def test_translate_keeps_declared_when_no_details(self):
        """无 details(可选字段没给)→ 无法重算,信任 LLM 声明值原样保留。"""
        c = BlindComparator()
        raw = ComparisonResult.model_validate({
            "winner": "A", "reasoning": "x", "rubric": {}, "output_quality": {},
            "expectation_results": {"A": {"passed": 9, "total": 11, "pass_rate": 0.82}},
        })
        out = c._translate(raw, side0_is_posA=True)
        er = out.expectation_results["with"]
        assert er.passed == 9
        assert er.total == 11
        assert er.pass_rate == 0.82

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", return_value=VALID_COMPARISON_JSON)
    def test_compare_end_to_end(self, mock_claude):
        c = BlindComparator(rng=_FixedRng(True))  # with 放 A 位
        result = c.compare("with-output", "without-output", "eval prompt", ["exp 1"])
        # LLM 判 position A 胜;with 在 A → 胜者翻成 with
        assert result.winner == "with"
        assert result.rubric["with"].overall_score == 9.0
        assert result.pos_a_label == "with"    # 揭盲标注随结果存下,供 viewer 标 A/B 映射
        assert result.pos_b_label == "without"
        mock_claude.assert_called_once()

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", return_value=VALID_COMPARISON_JSON)
    def test_compare_position_flip_flips_winner(self, mock_claude):
        # 同一份 LLM 响应(判 A 胜):with 放 A → with 胜;with 放 B → without 胜
        r_a = BlindComparator(rng=_FixedRng(True)).compare("w", "wo", "p")
        r_b = BlindComparator(rng=_FixedRng(False)).compare("w", "wo", "p")
        assert r_a.winner == "with"
        assert r_b.winner == "without"

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", return_value=VALID_COMPARISON_JSON)
    def test_compare_shuffle_swaps_prompt_position(self, mock_claude):
        # with_is_a=False → without 放 OUTPUT A、with 放 OUTPUT B(证明 position 真打乱)
        BlindComparator(rng=_FixedRng(False)).compare("WITH-MARKER", "WITHOUT-MARKER", "p")
        sent = mock_claude.call_args[0][0]
        assert "OUTPUT A ---\nWITHOUT-MARKER" in sent
        assert "OUTPUT B ---\nWITH-MARKER" in sent

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", side_effect=_LLMRetryExhausted("exhausted"))
    def test_compare_call_failure(self, mock_claude):
        c = BlindComparator()
        result = c.compare("with", "without", "prompt")
        # _call 抛错 → _parse("") raw winner TIE → 翻成 tie
        assert result.winner == "tie"

    # ── failed 标志(让 _compare_with_retry 能识别失败并重试,修死循环 bug)──

    def test_parse_sets_failed_on_empty_response(self):
        c = BlindComparator()
        assert c._parse("").failed is True  # 调用失败 → ""

    def test_parse_sets_failed_on_garbage(self):
        c = BlindComparator()
        assert c._parse("not json, no braces at all").failed is True

    def test_parse_no_failed_on_valid(self):
        c = BlindComparator()
        assert c._parse(VALID_COMPARISON_JSON).failed is False

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", side_effect=_LLMRetryExhausted("exhausted"))
    def test_compare_marks_failed_on_call_failure(self, mock_claude):
        c = BlindComparator()
        result = c.compare("with", "without", "prompt")
        assert result.winner == "tie"  # 旧契约保留(降级 tie)
        assert result.failed is True   # 但标 failed → 可被 _compare_with_retry 重试

    # ── label 参数化(块 B:v1/v2 双 skill)──

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", return_value=VALID_COMPARISON_JSON)
    def test_compare_v1v2_labels(self, mock_claude):
        """label_a/b 驱动 winner + dict key 语义:v1/v2 替代 with/without。"""
        c = BlindComparator(rng=_FixedRng(True))  # side0(output_a)放 A 位
        result = c.compare("v1-output", "v2-output", "eval prompt", ["exp"],
                           label_a="v1", label_b="v2")
        # LLM 判 position A 胜;A 位是 side0 → 胜者 = label_a = v1
        assert result.winner == "v1"
        assert result.rubric["v1"].overall_score == 9.0   # A → label_a
        assert result.rubric["v2"].overall_score == 5.4   # B → label_b
        assert "with" not in result.rubric and "without" not in result.rubric

    @patch("skill_eval.scoring.comparator.call_llm_with_retry", return_value=VALID_COMPARISON_JSON)
    def test_compare_default_labels_unchanged(self, mock_claude):
        """默认 label_a/b=with/without → 行为与改前一致(向后兼容)。"""
        c = BlindComparator(rng=_FixedRng(True))
        result = c.compare("with-output", "without-output", "eval prompt")
        assert result.winner == "with"
        assert result.rubric["with"].overall_score == 9.0


class TestPosthocAnalyzer:
    def test_loads_prompt_template(self):
        a = PosthocAnalyzer()
        assert a._prompt_template != ""
        assert "Post-hoc Analyzer" in a._prompt_template

    def test_build_benchmark_prompt(self):
        a = PosthocAnalyzer()
        prompt = a._build_benchmark_prompt('{"runs": []}', "test skill")
        assert "BENCHMARK DATA" in prompt
        assert "test skill" in prompt

    def test_build_comparison_prompt(self):
        a = PosthocAnalyzer()
        prompt = a._build_comparison_prompt(
            "A", "winner skill content", "loser skill content",
            "A was better", "winner transcript", "loser transcript",
            focus_skill="focus skill content",
        )
        assert "WINNER SKILL" in prompt
        assert "LOSER SKILL" in prompt
        assert "A was better" in prompt
        # focus_skill 作为改进对象显式注入(独立段)
        assert "FOCUS SKILL" in prompt
        assert "focus skill content" in prompt

    def test_instruction_following_score_allows_null(self):
        """无 skill 侧(without)遵循度 N/A:score=null(不打分),非假 0/10。
        model 必须接受 score=None(LLM 产 null 时解析为 None,viewer 据此显 N/A)。"""
        from skill_eval.models.analysis_models import AnalysisInstructionFollowing
        na = AnalysisInstructionFollowing(score=None, issues=["N/A — 无 skill 基线"])
        dumped = na.model_dump()
        assert dumped["score"] is None
        assert dumped["issues"] == ["N/A — 无 skill 基线"]
        # 有 skill 侧正常打分(0-10 int)不受影响
        assert AnalysisInstructionFollowing(score=8, issues=[]).model_dump()["score"] == 8

    def test_analyzer_prompt_focus_skill_and_null_score_rules(self):
        """analyzer.md Mode 2:加 FOCUS SKILL(改进对象)+ 无 skill 侧 score=null 规则,
        guideline 从 'improve the losing skill' 改成针对 FOCUS SKILL。
        RED:旧 prompt 无 FOCUS SKILL 段、仍说 losing skill、无 null 规则。"""
        a = PosthocAnalyzer()
        p = a._prompt_template
        assert "FOCUS SKILL" in p                       # 新增:改进对象显式段
        assert "improve the losing skill" not in p      # 旧 guideline 换掉
        # 无 skill 侧(LOSER SKILL = (no skill))遵循度 N/A 规则
        assert "(no skill)" in p and "null" in p.lower()

    def test_parse_notes_valid_array(self):
        a = PosthocAnalyzer()
        notes = a._parse_notes(json.dumps(["note 1", "note 2"]))
        assert notes == ["note 1", "note 2"]

    def test_parse_notes_array_in_text(self):
        a = PosthocAnalyzer()
        notes = a._parse_notes(f'Here:\n{json.dumps(["a", "b"])}\nDone.')
        assert notes == ["a", "b"]

    def test_parse_notes_empty(self):
        a = PosthocAnalyzer()
        notes = a._parse_notes("")
        assert notes == []

    def test_parse_notes_fallback_lines(self):
        a = PosthocAnalyzer()
        notes = a._parse_notes("- first observation\n- second observation\n# header")
        assert "first observation" in notes
        assert "second observation" in notes
        assert len(notes) == 2

    def test_parse_analysis_valid(self):
        a = PosthocAnalyzer()
        result = a._parse_analysis(VALID_ANALYSIS_JSON)
        assert len(result.winner_strengths) == 2
        assert result.instruction_following["winner"].score == 9

    def test_parse_analysis_empty(self):
        a = PosthocAnalyzer()
        result = a._parse_analysis("")
        assert result is None

    @patch("skill_eval.scoring.analyzer.call_llm_with_retry", return_value='["note 1", "note 2"]')
    def test_analyze_benchmark_end_to_end(self, mock_claude):
        a = PosthocAnalyzer()
        notes = a.analyze_benchmark('{"benchmark": true}', "test skill")
        assert notes == ["note 1", "note 2"]

    @patch("skill_eval.scoring.analyzer.call_llm_with_retry", return_value=VALID_ANALYSIS_JSON)
    def test_analyze_comparison_end_to_end(self, mock_claude):
        a = PosthocAnalyzer()
        result = a.analyze_comparison(
            "A", "winner skill", "loser skill",
            "A was better", "winner transcript", "loser transcript",
            focus_skill="focus skill",
        )
        assert len(result.winner_strengths) == 2
        assert result.improvement_suggestions[0].priority == "high"
