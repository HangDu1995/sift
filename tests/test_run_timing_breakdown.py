# tests/test_run_timing_breakdown.py
"""run 耗时正确性:total 必须严格 = phase1_wall + phase2_wall(加总承诺),且 phase2 含 aggregate
内耗时(修旧 bug:旧 total 在 aggregate 前捕获,不含 comparator/analyzer 尾巴)。"""
from __future__ import annotations

from pathlib import Path


def _make_iteration_dir(tmp_path: Path) -> Path:
    """最小可聚合 iteration_dir:一侧(with_skill)一 eval 一 run,含 grading.json + timing.json。"""
    run = tmp_path / "with_skill" / "eval-1" / "run-1"
    run.mkdir(parents=True)
    (run / "grading.json").write_text(
        '{"summary":{"pass_rate":1.0,"passed":1,"failed":0,"total":1,'
        '"pass_rate_programmatic":1.0,"pass_rate_llm":1.0},'
        '"expectations":[{"text":"x","passed":true,"evidence":"","source":"programmatic"}],'
        '"execution_metrics":{"total_tool_calls":0}}',
        encoding="utf-8",
    )
    (run / "timing.json").write_text(
        '{"total_tokens":10,"total_duration_seconds":1.0,"grader_token_usage":{}}',
        encoding="utf-8",
    )
    return tmp_path


def test_total_equals_phase1_plus_phase2_and_includes_tail(tmp_path, monkeypatch):
    """total 严格 = phase1_wall + phase2_wall;phase2 含 aggregate 内耗时(尾巴进 total)。"""
    from skill_eval.execution import benchmark as bm

    it = _make_iteration_dir(tmp_path)
    agg = bm.BenchmarkAggregator(enable_llm=False)
    # 隔离掉 LLM/外部依赖:comparisons/analysis 不真跑(本测只关心墙钟分解)
    monkeypatch.setattr(bm, "run_comparisons", lambda *a, **k: [])
    monkeypatch.setattr(bm, "run_analysis", lambda *a, **k: ([], None))

    b = agg.aggregate(it, Path(), phase1_wall=0.0)
    du = b.llm_duration_seconds

    assert "phase1_wall" in du and "phase2_wall" in du
    # 加总承诺:total = phase1 + phase2(严格,不是 ≥)
    assert abs(du["total"] - (du["phase1_wall"] + du["phase2_wall"])) < 0.05
    # aggregate 内有耗时(读 run 产物等)→ phase2 非零;旧 bug:total 在 aggregate 前捕获,phase2 不在其中
    assert du["phase2_wall"] > 0.0


def test_phase2_subwalls_measured(tmp_path, monkeypatch):
    """phase2 子墙钟:comparator_wall 给整段计时(非累计和);read_runs_wall 非负;子项 ≤ phase2。"""
    import time as _time
    from skill_eval.execution import benchmark as bm

    it = _make_iteration_dir(tmp_path)
    agg = bm.BenchmarkAggregator(enable_llm=True)

    def slow_cmp(*a, **k):
        _time.sleep(0.3)  # 模拟比较阶段墙钟(累计和因不调 on_duration 仍为 0 → 区分两维度)
        return []

    monkeypatch.setattr(bm, "run_comparisons", slow_cmp)
    monkeypatch.setattr(bm, "run_analysis", lambda *a, **k: ([], None))

    b = agg.aggregate(it, Path(), phase1_wall=0.0)
    du = b.llm_duration_seconds
    assert du.get("comparator_wall", 0) >= 0.25        # 阶段墙钟(整段计时)
    assert du.get("read_runs_wall", -1) >= 0           # 读产物程序段
    assert du.get("analyzer_wall", -1) >= 0
    assert du["comparator_wall"] <= du["phase2_wall"]  # 子项 ≤ 阶段

