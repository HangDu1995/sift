"""Tests for overfitting detection: splitter, gap calculator, detector."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skill_eval.models import EvalCase, EvalSet, OverfittingReport
from skill_eval.overfitting.splitter import EvalSplitter
from skill_eval.overfitting.gap import GapCalculator
from skill_eval.overfitting.detector import OverfittingDetector


@pytest.fixture
def sample_eval_set() -> EvalSet:
    return EvalSet(skill_name="test-skill", evals=[
        EvalCase(id=1, name="L1-test", prompt="do something", level="L1", expectations=["expect 1"]),
        EvalCase(id=2, name="L2-test-1", prompt="do more", level="L2", expectations=["expect 2"]),
        EvalCase(id=3, name="L2-test-2", prompt="do more", level="L2", expectations=["expect 3"]),
        EvalCase(id=4, name="L2-test-3", prompt="even more", level="L2", expectations=["expect 4"]),
    ])


class TestEvalSplitter:
    def test_split_preserves_level_distribution(self, sample_eval_set):
        splitter = EvalSplitter()
        train, holdout = splitter.split(sample_eval_set, holdout_fraction=0.5, seed=42)

        assert len(train.evals) + len(holdout.evals) == 4
        assert len(train.evals) >= 1
        assert len(holdout.evals) >= 1
        assert train.skill_name == "test-skill"

        train_ids = {e.id for e in train.evals}
        holdout_ids = {e.id for e in holdout.evals}
        assert train_ids.isdisjoint(holdout_ids)

    def test_split_reproducible(self, sample_eval_set):
        splitter = EvalSplitter()
        train1, holdout1 = splitter.split(sample_eval_set, seed=42)
        train2, holdout2 = splitter.split(sample_eval_set, seed=42)

        assert [e.id for e in train1.evals] == [e.id for e in train2.evals]
        assert [e.id for e in holdout1.evals] == [e.id for e in holdout2.evals]

    def test_split_minimum_holdout(self, sample_eval_set):
        splitter = EvalSplitter()
        train, holdout = splitter.split(sample_eval_set, holdout_fraction=0.1, seed=42)
        assert len(holdout.evals) >= 1
        assert len(train.evals) >= 2

    def test_split_too_few_evals(self):
        splitter = EvalSplitter()
        tiny = EvalSet(skill_name="test", evals=[EvalCase(id=1, name="t", prompt="p", level="L1")])
        with pytest.raises(ValueError, match="at least 2"):
            splitter.split(tiny)

    def test_split_invalid_fraction(self, sample_eval_set):
        splitter = EvalSplitter()
        with pytest.raises(ValueError):
            splitter.split(sample_eval_set, holdout_fraction=0)
        with pytest.raises(ValueError):
            splitter.split(sample_eval_set, holdout_fraction=1)

    def test_save_and_load(self, sample_eval_set, tmp_path):
        splitter = EvalSplitter()
        path = tmp_path / "evals.json"
        splitter.save(sample_eval_set, path)
        loaded = splitter.load(path)
        assert loaded.skill_name == "test-skill"
        assert len(loaded.evals) == 4


class TestGapCalculator:
    def test_positive_gap(self):
        calc = GapCalculator()
        train = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        holdout = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.6}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.30"},
            }
        }
        gap = calc.calculate(train, holdout)
        assert gap.train_delta == 0.50
        assert gap.holdout_delta == 0.30
        assert gap.gap == 0.20
        assert gap.verdict == "overfit"

    def test_good_gap(self):
        calc = GapCalculator()
        train = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        holdout = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.77}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.47"},
            }
        }
        gap = calc.calculate(train, holdout)
        assert gap.verdict == "good"

    def test_warn_gap(self):
        calc = GapCalculator()
        train = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        holdout = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.7}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.35"},
            }
        }
        gap = calc.calculate(train, holdout)
        assert gap.verdict == "warn"

    def test_empty_delta_string(self):
        calc = GapCalculator()
        train = {"run_summary": {"delta": {"pass_rate": ""}}}
        holdout = {"run_summary": {"delta": {"pass_rate": ""}}}
        gap = calc.calculate(train, holdout)
        assert gap.gap == 0.0


class TestOverfittingDetector:
    def test_parse_empty(self):
        d = OverfittingDetector()
        result = d._parse("")
        assert result == []

    def test_parse_valid(self):
        d = OverfittingDetector()
        response = json.dumps([{
            "line": 12,
            "snippet": "NEVER use List",
            "matched_eval_id": 3,
            "matched_expectation": "输出使用 MutableList",
            "reason": "指令精确匹配 expectation",
            "severity": "high",
        }])
        result = d._parse(response)
        assert len(result) == 1
        assert result[0].severity == "high"

    def test_parse_invalid(self):
        d = OverfittingDetector()
        result = d._parse("not json at all")
        assert result == []

    def test_build_report_no_data(self):
        d = OverfittingDetector()
        report = d.build_report(skill_name="test")
        assert report.overfitting_score == 0.0
        assert report.generalization_gap is None

    def test_build_report_with_gap(self):
        d = OverfittingDetector()
        report = d.build_report(
            skill_name="test",
            gap_data={"verdict": "overfit", "gap": 0.25},
        )
        assert report.overfitting_score >= 0.7
        assert report.generalization_gap.verdict == "overfit"

    def test_build_report_with_suspicious(self):
        d = OverfittingDetector()
        from skill_eval.models import SuspiciousInstruction
        suspicious = [
            SuspiciousInstruction(severity="high", line=5, snippet="NEVER do X", reason="matches eval"),
            SuspiciousInstruction(severity="medium", line=10, snippet="use Y", reason="matches eval"),
        ]
        report = d.build_report(
            skill_name="test",
            suspicious=suspicious,
            gap_data={"verdict": "overfit", "gap": 0.30},
        )
        assert report.overfitting_score >= 0.7
        assert len(report.suspicious_instructions) == 2

    def test_detect_without_skill_path(self):
        d = OverfittingDetector()
        result = d.detect(skill_path=None, skill_content="", train_evals="")
        assert result == []

    @patch("skill_eval.overfitting.detector.call_claude")
    def test_detect_with_skill(self, mock_claude):
        mock_claude.return_value = json.dumps([])
        d = OverfittingDetector()
        result = d.detect(skill_content="---\nname: test\n---\n\n# Test", train_evals="{}")
        assert result == []
        assert mock_claude.called


class TestOverfittingReportModel:
    def test_default(self):
        r = OverfittingReport(skill_name="test")
        assert r.overfitting_score == 0.0
        assert r.generalization_gap is None
        assert r.suspicious_instructions == []
