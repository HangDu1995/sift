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
        "A": {"passed": 4, "total": 5, "pass_rate": 0.8, "details": [{"text": "has name", "passed": True}]},
        "B": {"passed": 3, "total": 5, "pass_rate": 0.6, "details": [{"text": "has name", "passed": True}]},
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
        assert "failed" in result.reasoning.lower()

    def test_parse_invalid_json(self):
        c = BlindComparator()
        result = c._parse("not json at all")
        assert result.winner == "TIE"

    @patch("skill_eval.scoring.comparator.call_claude", return_value=VALID_COMPARISON_JSON)
    def test_compare_end_to_end(self, mock_claude):
        c = BlindComparator()
        result = c.compare("output A", "output B", "eval prompt", ["exp 1"])
        assert result.winner == "A"
        mock_claude.assert_called_once()

    @patch("skill_eval.scoring.comparator.call_claude", side_effect=RuntimeError("timeout"))
    def test_compare_call_failure(self, mock_claude):
        c = BlindComparator()
        result = c.compare("A", "B", "prompt")
        assert result.winner == "TIE"


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
        )
        assert "WINNER SKILL" in prompt
        assert "LOSER SKILL" in prompt
        assert "A was better" in prompt

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

    @patch("skill_eval.scoring.analyzer.call_claude", return_value='["note 1", "note 2"]')
    def test_analyze_benchmark_end_to_end(self, mock_claude):
        a = PosthocAnalyzer()
        notes = a.analyze_benchmark('{"benchmark": true}', "test skill")
        assert notes == ["note 1", "note 2"]

    @patch("skill_eval.scoring.analyzer.call_claude", return_value=VALID_ANALYSIS_JSON)
    def test_analyze_comparison_end_to_end(self, mock_claude):
        a = PosthocAnalyzer()
        result = a.analyze_comparison(
            "A", "winner skill", "loser skill",
            "A was better", "winner transcript", "loser transcript",
        )
        assert len(result.winner_strengths) == 2
        assert result.improvement_suggestions[0].priority == "high"
