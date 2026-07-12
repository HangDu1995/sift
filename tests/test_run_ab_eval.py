"""块 B:pipeline._run_ab_eval 接线 —— build_sides 构建 sides 透给 runner + aggregator。"""

from pathlib import Path
from unittest.mock import patch

from skill_eval.cli.pipeline import _run_ab_eval
from skill_eval.models import EvalSet, EvalCase, BenchmarkData, BenchmarkMetadata


def _skill(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir()
    (p / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: t\n---\nbody\n", encoding="utf-8"
    )
    return p


def _wire(tmp_path: Path, captured: dict):
    """返回 (fake_run_ab_test, fake_aggregate);iteration_dir 预建。捕获 sides。"""
    iter_dir = tmp_path / "out" / "iteration-1"
    iter_dir.mkdir(parents=True)

    def fake_run_ab_test(self, skill_path, eval_set, sides=None):
        captured["runner_sides"] = sides
        return iter_dir

    def fake_aggregate(self, iteration_dir, skill_path=Path(), sides=None):
        captured["agg_sides"] = sides
        return BenchmarkData(metadata=BenchmarkMetadata(skill_name="sk"))

    return fake_run_ab_test, fake_aggregate


def test_v1v2_builds_and_threads_sides(tmp_path):
    skill_a = _skill(tmp_path, "v1")
    skill_b = _skill(tmp_path, "v2")
    eval_set = EvalSet(skill_name="v1", evals=[EvalCase(id=1, prompt="do")])
    captured: dict = {}
    fake_run, fake_agg = _wire(tmp_path, captured)

    with patch("skill_eval.execution.runner.ABTestRunner.run_ab_test", fake_run), \
         patch("skill_eval.execution.benchmark.BenchmarkAggregator.aggregate", fake_agg):
        _run_ab_eval(
            skill_path=skill_a, eval_set=eval_set, runs=1, model=None, workers=1,
            timeout=10, max_turns=2, scoring="hybrid", no_calibration=True,
            skip_compliance=True, output_dir=tmp_path / "out", fmt="json",
            skill_b=skill_b, label_a="v1", label_b="v2",
        )

    expected = [("v1", skill_a, "v1"), ("v2", skill_b, "v2")]
    assert captured["runner_sides"] == expected, f"runner sides 错:{captured['runner_sides']}"
    assert captured["agg_sides"] == expected, f"agg sides 错:{captured['agg_sides']}"


def test_default_no_skill_b_threads_with_without_sides(tmp_path):
    """无 skill_b → build_sides 默认 with/without(显式构建,非 None)。"""
    skill = _skill(tmp_path, "sk")
    eval_set = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="do")])
    captured: dict = {}
    fake_run, fake_agg = _wire(tmp_path, captured)

    with patch("skill_eval.execution.runner.ABTestRunner.run_ab_test", fake_run), \
         patch("skill_eval.execution.benchmark.BenchmarkAggregator.aggregate", fake_agg):
        _run_ab_eval(
            skill_path=skill, eval_set=eval_set, runs=1, model=None, workers=1,
            timeout=10, max_turns=2, scoring="hybrid", no_calibration=True,
            skip_compliance=True, output_dir=tmp_path / "out", fmt="json",
        )

    expected = [("with_skill", skill, "with"), ("without_skill", None, "without")]
    assert captured["runner_sides"] == expected
    assert captured["agg_sides"] == expected
