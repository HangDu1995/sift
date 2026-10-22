"""aggregate 汇 llm_duration_seconds——墙钟 total 严格 = phase1_wall + phase2_wall;
分列(scoring/grader/.../executor)是并发叠加的调用时长加总,与墙钟是两个维度(可 > total)。
"""

import json

from sift.execution.benchmark import BenchmarkAggregator


def _make_run(run_dir, total_tokens=100, grader_dur=None, compliance_dur=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    timing = {"total_tokens": total_tokens, "total_duration_seconds": 0.5}
    if grader_dur is not None:
        timing["grader_duration_seconds"] = grader_dur
    (run_dir / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
    (run_dir / "grading.json").write_text(
        '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}', encoding="utf-8"
    )
    if compliance_dur is not None:
        (run_dir / "compliance.json").write_text(
            json.dumps(
                {
                    "duration_seconds": compliance_dur,
                    "token_usage": {},
                    "summary": {},
                    "findings": [],
                    "by_instruction": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )


def test_aggregate_populates_llm_duration(tmp_path):
    """分列=scoring(grader+comparator+analyzer)+ audit(compliance)+ executor(timing);
    墙钟:total = phase1_wall + phase2_wall。
    """
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

    with (
        patch("sift.execution.benchmark.run_comparisons", side_effect=fake_run_comparisons),
        patch("sift.execution.benchmark.run_analysis", side_effect=fake_run_analysis),
    ):
        bench = agg.aggregate(tmp_path, phase1_wall=15.0)
    du = bench.llm_duration_seconds
    # 分列(并发叠加,调用时长加总)
    assert du["scoring"] == 2.0 + 1.5 + 4.0  # grader + comparator + analyzer(合计)
    assert du["grader"] == 2.0 and du["comparator"] == 1.5 and du["analyzer"] == 4.0  # scoring 细分
    assert du["audit"] == 3.0  # compliance rollup
    assert du["executor"] == 0.5  # timing total_duration_seconds
    # 墙钟分解(加总 = total)
    assert du["phase1_wall"] == 15.0
    # round(3) 下亚毫秒的 phase2（mock 瞬回 + 热缓存）会归 0.0——物理下限即可，
    # 正性由下一行加总不变量与真实跑（无 mock）覆盖
    assert du["phase2_wall"] >= 0.0
    assert du["total"] == du["phase1_wall"] + du["phase2_wall"]


def test_aggregate_duration_total_not_sum_of_segments(tmp_path):
    """墙钟 total 与分列和是两个维度:分列(并行叠加)可 > total(phase1+phase2 墙钟)。"""
    from unittest.mock import patch

    cfg = tmp_path / "with_skill"
    _make_run(cfg / "eval-1" / "run-1", total_tokens=100, grader_dur=10.0, compliance_dur=10.0)
    agg = BenchmarkAggregator(model=None)
    with patch("sift.execution.benchmark.run_comparisons"), patch("sift.execution.benchmark.run_analysis"):
        bench = agg.aggregate(tmp_path, phase1_wall=5.0)
    du = bench.llm_duration_seconds
    assert du["total"] == du["phase1_wall"] + du["phase2_wall"]  # 墙钟加总
    assert du["phase1_wall"] == 5.0
    assert du["scoring"] + du["audit"] + du["executor"] > du["total"]  # 分列和 > 墙钟(并行)
