"""Phase 2: BenchmarkAggregator.aggregate 汇五路 token → llm_token_usage。"""
import json
from pathlib import Path

from skill_eval.execution.benchmark import BenchmarkAggregator


def _make_run(run_dir: Path, total_tokens=100, grader_usage=None, compliance_usage=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    timing = {"total_tokens": total_tokens, "duration_ms": 10, "total_duration_seconds": 0.01}
    if grader_usage is not None:
        timing["grader_token_usage"] = grader_usage
    (run_dir / "timing.json").write_text(json.dumps(timing), encoding="utf-8")
    (run_dir / "grading.json").write_text(
        '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}',
        encoding="utf-8")
    if compliance_usage is not None:
        (run_dir / "compliance.json").write_text(
            json.dumps({"token_usage": compliance_usage, "summary": {}, "findings": [],
                        "by_instruction": [], "warnings": []}), encoding="utf-8")


def test_aggregate_populates_llm_token_usage(tmp_path):
    """scoring(grader)+ audit(rollup compliance)+ executor(timing)+ total。suite mock 免真 LLM。"""
    from unittest.mock import patch
    cfg = tmp_path / "with_skill"
    _make_run(cfg / "eval-1" / "run-1", total_tokens=100,
              grader_usage={"input_tokens": 30, "output_tokens": 10, "total_cost_usd": 0.01, "calls": 1},
              compliance_usage={"input_tokens": 40, "output_tokens": 5, "total_cost_usd": 0.02, "calls": 1})
    agg = BenchmarkAggregator(model=None)
    with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
         patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
        bench = agg.aggregate(tmp_path)
    tu = bench.llm_token_usage
    assert "scoring" in tu and "audit" in tu and "executor" in tu and "total" in tu
    assert tu["scoring"]["input_tokens"] == 30   # grader
    assert tu["audit"]["input_tokens"] == 40      # compliance rollup
    assert tu["executor"]["input_tokens"] == 100  # timing total_tokens
    assert tu["total"]["input_tokens"] == 170     # 30+40+100
    assert abs(tu["total"]["total_cost_usd"] - 0.03) < 1e-9   # scoring+audit(executor 无 cost)


def test_aggregate_llm_token_usage_empty_when_no_llm(tmp_path):
    """无 grader_token_usage / compliance → scoring/audit 空;仅 executor。"""
    from unittest.mock import patch
    cfg = tmp_path / "with_skill"
    _make_run(cfg / "eval-1" / "run-1", total_tokens=50)  # 无 grader/compliance
    agg = BenchmarkAggregator(model=None)
    with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
         patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
        bench = agg.aggregate(tmp_path)
    tu = bench.llm_token_usage
    assert tu.get("scoring", {}) == {}
    assert tu.get("audit", {}) == {}
    assert tu["executor"]["input_tokens"] == 50
    assert tu["total"]["input_tokens"] == 50
