"""Tests for eval loader and execution engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

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
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        assert len(benchmark.runs) == 2
        assert benchmark.run_summary["delta"]["pass_rate"] == "+1.00"

    def test_aggregate_empty(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        iteration.mkdir()

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration)

        assert benchmark.runs == []

    def test_aggregate_survives_comparison_failure(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "expectations": [{"text": "x", "passed": True, "evidence": ""}],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.0, "passed": 0, "failed": 2, "total": 2},
            "expectations": [{"text": "x", "passed": False, "evidence": ""}],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", side_effect=RuntimeError("LLM timeout")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert "with_skill" in benchmark.run_summary
        assert benchmark.comparisons is None or benchmark.comparisons == []

    def test_aggregate_survives_analysis_failure(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "expectations": [],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.5, "passed": 1, "failed": 1, "total": 2},
            "expectations": [],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", side_effect=RuntimeError("LLM rate limited")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert benchmark.analysis is None

    def test_aggregate_survives_both_failures(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.8, "passed": 4, "failed": 1, "total": 5},
            "expectations": [],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.2, "passed": 1, "failed": 4, "total": 5},
            "expectations": [],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", side_effect=RuntimeError("fail1")), \
             patch("skill_eval.execution.benchmark.run_analysis", side_effect=RuntimeError("fail2")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert benchmark.comparisons is None or benchmark.comparisons == []
        assert benchmark.analysis is None


class TestCopySkillReferences:
    """_copy_skill_references:把 skill 的 references/ 拷进考场(P0-2 核心逻辑)."""

    @staticmethod
    def _make_skill(tmp_path: Path, with_refs: bool = True) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        if with_refs:
            refs = skill / "references"
            refs.mkdir()
            (refs / "guide.md").write_text("domain knowledge", encoding="utf-8")
        return skill

    def test_copies_references_into_work_dir(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=True)
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(skill, work)
        assert (work / "references" / "guide.md").read_text(encoding="utf-8") == "domain knowledge"

    def test_no_references_dir_is_noop(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=False)
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(skill, work)
        assert not (work / "references").exists()

    def test_does_not_overwrite_existing(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=True)
        work = tmp_path / "work"
        work.mkdir()
        (work / "references").mkdir()
        (work / "references" / "existing.md").write_text("keep me", encoding="utf-8")
        _copy_skill_references(skill, work)
        assert (work / "references" / "existing.md").read_text(encoding="utf-8") == "keep me"
        assert not (work / "references" / "guide.md").exists()

    def test_none_skill_path_is_noop(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(None, work)
        assert not (work / "references").exists()


class TestABTestRunnerReferencesAutoCopy:
    """P0-2 集成:references 依赖型 skill(未配 source_dir)开箱即用 ——
    with_skill 考场自动有 references/,without_skill 没有。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "myskill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: test skill\n---\n"
            "# 步骤\n\n参见 references/guide.md 了解领域知识。\n",
            encoding="utf-8",
        )
        (skill / "references").mkdir()
        (skill / "references" / "guide.md").write_text("domain knowledge", encoding="utf-8")
        return skill

    def test_with_skill_copies_references_without_does_not(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        skill = self._make_skill(tmp_path)

        # mock ClaudeRunner.run —— 不真跑 claude,返回最小 RunOutput
        def fake_run(self, prompt, skill_path=None, **kwargs):
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        config = RunConfig(
            runs_per_config=1,
            output_dir=tmp_path / "out",
            skip_compliance=True,
            skip_calibration=True,
            max_workers=1,
        )
        scoring = ScoringConfig(enable_programmatic=False, enable_llm=False)
        runner = ABTestRunner(config=config, scoring_config=scoring)
        eval_set = EvalSet(skill_name="myskill", evals=[
            EvalCase(id=1, prompt="do the task"),  # 故意不配 source_dir
        ])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        with_ref = tmp_path / "out" / "iteration-1" / "with_skill" / "eval-1" / "run-1" / "outputs" / "references" / "guide.md"
        without_ref = tmp_path / "out" / "iteration-1" / "without_skill" / "eval-1" / "run-1" / "outputs" / "references"
        assert with_ref.exists(), "with_skill 考场应自动拷 references/(body 引用了它)"
        assert not without_ref.exists(), "without_skill 不注入 body,不该拷 references/"


class TestABTestRunnerPerEvalTimeout:
    """P1-3a: per-eval timeout 覆盖全局(混跑时重任务给大、轻任务走默认)。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        return skill

    def test_per_eval_timeout_overrides_global(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        captured: list[int | None] = []

        def fake_run(self, prompt, skill_path=None, timeout=None, **kwargs):
            captured.append(timeout)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", timeout_seconds=300,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[
            EvalCase(id=1, prompt="simple"),               # 无 timeout → 全局 300
            EvalCase(id=2, prompt="design", timeout=600),  # per-eval → 600
        ])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        # 2 evals × 2 configs = 4 runs;eval1 全 300,eval2 全 600
        assert 300 in captured, "无 per-eval timeout 的 eval 应走全局 300"
        assert 600 in captured, "有 per-eval timeout 的 eval 应覆盖为 600"
        assert all(t in (300, 600) for t in captured), f"意外 timeout: {captured}"

    def test_no_per_eval_timeout_falls_back_to_global(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        captured: list[int | None] = []

        def fake_run(self, prompt, skill_path=None, timeout=None, **kwargs):
            captured.append(timeout)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", timeout_seconds=250,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x")])  # 都不配 timeout
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        assert all(t == 250 for t in captured), f"无 per-eval 应全部走全局 250,实际: {captured}"
