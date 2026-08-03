"""benchmark run_summary.delta:raw pass_rate delta + NormalizedGain(天花板修正)。"""
from skill_eval.execution.benchmark import BenchmarkAggregator


def _entry(pass_rate, **kw):
    e = {"pass_rate": pass_rate, "pass_rate_programmatic": pass_rate,
         "pass_rate_llm": None, "time_seconds": 1.0, "tokens": 100}
    e.update(kw)
    return e


def test_delta_raw_and_normalized_gain():
    """raw delta 0.40(0.9-0.5);normalized_gain(0.5→0.9)=0.4/0.5=0.80(放大)。"""
    agg = BenchmarkAggregator(model=None)
    summary = agg._build_summary({
        "with_skill": [_entry(0.9)],
        "without_skill": [_entry(0.5)],
    })
    assert summary["delta"]["pass_rate"] == "+0.40"
    assert summary["delta"]["pass_rate_normalized_gain"] == "+0.80"


def test_delta_normalized_gain_high_ceiling_amplifies():
    """高分段:0.95→0.99,raw 0.04,gain ~0.80(raw 系统性低估高位改进)。"""
    agg = BenchmarkAggregator(model=None)
    summary = agg._build_summary({
        "with_skill": [_entry(0.99)],
        "without_skill": [_entry(0.95)],
    })
    assert summary["delta"]["pass_rate"] == "+0.04"
    # normalized_gain(0.95, 0.99) = 0.04/0.05 = 0.80
    assert summary["delta"]["pass_rate_normalized_gain"] == "+0.80"


def test_delta_normalized_gain_negative_for_regression():
    """退步:0.6→0.5,raw -0.10,gain -0.25。"""
    agg = BenchmarkAggregator(model=None)
    summary = agg._build_summary({
        "with_skill": [_entry(0.5)],
        "without_skill": [_entry(0.6)],
    })
    assert summary["delta"]["pass_rate"] == "-0.10"
    # normalized_gain(0.6, 0.5) = -0.1/0.4 = -0.25
    assert summary["delta"]["pass_rate_normalized_gain"] == "-0.25"


def test_no_delta_when_single_config():
    """单 config(无 A/B)→ 不产 delta。"""
    agg = BenchmarkAggregator(model=None)
    summary = agg._build_summary({"with_skill": [_entry(0.9)]})
    assert "delta" not in summary
