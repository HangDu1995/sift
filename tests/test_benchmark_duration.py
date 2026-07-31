"""Phase 2.5: aggregate 汇 llm_duration_seconds——total=wall_duration(独立测,非分列和)。"""
import json
from pathlib import Path
from skill_eval.execution.benchmark import BenchmarkAggregator


def _make_run(run_dir, total_tokens=100, grader_dur=None, compliance_dur=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    timing = {"total_tokens": total_tokens, "total_duration_seconds": 0.5}
    if grader_dur is not None:
        timing["grader_duration_seconds"] = grader_dur
    (run_dir / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
    (run_dir / "grading.json").write_text(
        '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}', encoding="utf-8")
    if compliance_dur is not None:
        (run_dir / "compliance.json").write_text(
            json.dumps({"duration_seconds": compliance_dur, "token_usage": {}, "summary": {},
                        "findings": [], "by_instruction": [], "warnings": []}), encoding="utf-8")


def test_aggregate_populates_llm_duration(tmp_path):
    """scoring(grader+comparator+analyzer)+ audit(compliance)+ executor(timing)+ total(墙钟)。"""
    from unittest.mock import patch
    cfg = tmp_path / "with_skill"
    _make_run(cfg / "eval-1" / "run-1", total_tokens=100, grader_dur=2.0, compliance_dur=3.0)
    agg = BenchmarkAggregator(model=None)

    def fake_run_comparisons(aggregator, iteration_dir, sides=None, on_usage=None, on_duration=None):
        if on_duration:
            on_duration(1.5)  # comparator 时长
        return []

    def fake_run_analysis(aggregator, iteration_dir, benchmark, on_usage=None, on_duration=None):
        if on_duration:
            on_duration(4.0)  # analyzer 时长
        return [], None

    with patch("skill_eval.execution.benchmark.run_comparisons", side_effect=fake_run_comparisons), \
         patch("skill_eval.execution.benchmark.run_analysis", side_effect=fake_run_analysis):
        bench = agg.aggregate(tmp_path, wall_duration=15.0)
    du = bench.llm_duration_seconds
    assert du["scoring"] == 2.0 + 1.5 + 4.0   # grader + comparator + analyzer
    assert du["audit"] == 3.0                  # compliance rollup
    assert du["executor"] == 0.5               # timing total_duration_seconds
    assert du["total"] == 15.0                 # 墙钟(独立测,≠ 分列和)


def test_aggregate_duration_total_not_sum_of_segments(tmp_path):
    """核心约束:total ≠ 分列和(并行所致),total 独立 wall_duration。"""
    from unittest.mock import patch
    cfg = tmp_path / "with_skill"
    _make_run(cfg / "eval-1" / "run-1", total_tokens=100, grader_dur=10.0, compliance_dur=10.0)
    agg = BenchmarkAggregator(model=None)
    with patch("skill_eval.execution.benchmark.run_comparisons"), \
         patch("skill_eval.execution.benchmark.run_analysis"):
        bench = agg.aggregate(tmp_path, wall_duration=5.0)
    du = bench.llm_duration_seconds
    assert du["total"] == 5.0                                 # 墙钟
    assert du["scoring"] + du["audit"] + du["executor"] > du["total"]  # 分列和 > total(并行)
