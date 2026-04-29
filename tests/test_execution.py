"""Tests for eval loader and execution engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.evals.loader import EvalLoader
from skill_eval.errors import EvalValidationError
from skill_eval.execution.benchmark import BenchmarkAggregator, calculate_stats
from skill_eval.models import EvalSet, StatsValue


class TestEvalLoader:
    def test_load_valid(self, sample_evals_path: Path):
        loader = EvalLoader()
        eval_set = loader.load(sample_evals_path)
        assert eval_set.skill_name == "sample-skill"
        assert len(eval_set.evals) == 2

    def test_load_missing_file(self, tmp_path: Path):
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="not found"):
            loader.load(tmp_path / "nope.json")

    def test_load_invalid_json(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text("not json{{{", encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="Invalid JSON"):
            loader.load(p)

    def test_load_missing_skill_name(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"evals": [{"id": 1, "prompt": "x"}]}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="skill_name"):
            loader.load(p)

    def test_load_empty_skill_name(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"skill_name": "", "evals": [{"id": 1, "prompt": "x"}]}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="non-empty"):
            loader.load(p)

    def test_load_empty_evals(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"skill_name": "test", "evals": []}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="non-empty"):
            loader.load(p)

    def test_load_duplicate_ids(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}, {"id": 1, "prompt": "b"}],
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="Duplicate"):
            loader.load(p)

    def test_load_empty_prompt(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": ""}],
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="prompt"):
            loader.load(p)


class TestCalculateStats:
    def test_empty(self):
        s = calculate_stats([])
        assert s.mean == 0.0

    def test_single(self):
        s = calculate_stats([0.5])
        assert s.mean == 0.5
        assert s.stddev == 0.0

    def test_multiple(self):
        s = calculate_stats([1.0, 2.0, 3.0])
        assert s.mean == 2.0
        assert s.stddev > 0


class TestBenchmarkAggregator:
    def test_aggregate_from_iteration(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        iteration.mkdir(parents=True)

        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            config_dir.mkdir(parents=True)
            (config_dir / "outputs").mkdir()
            (config_dir / "grading.json").write_text(json.dumps({
                "expectations": [{"text": "t", "passed": config == "with_skill", "evidence": "e"}],
                "summary": {"passed": 1 if config == "with_skill" else 0, "failed": 0 if config == "with_skill" else 1, "total": 1, "pass_rate": 1.0 if config == "with_skill" else 0.0},
            }), encoding="utf-8")
            (config_dir / "timing.json").write_text(json.dumps({
                "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
            }), encoding="utf-8")

        agg = BenchmarkAggregator()
        benchmark = agg.aggregate(iteration, tmp_path)

        assert len(benchmark.runs) == 2
        assert benchmark.run_summary["delta"]["pass_rate"] == "+1.00"

    def test_aggregate_empty(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        iteration.mkdir()

        agg = BenchmarkAggregator()
        benchmark = agg.aggregate(iteration)

        assert benchmark.runs == []
