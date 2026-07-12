"""gate 判决逻辑纯函数测试(块 C)。"""
from skill_eval.gating.verdict import evaluate_gate, parse_delta


def _bench(delta_prog, comparison_summary=None):
    """构造最小 benchmark dict;delta_prog=None → 该项缺省。"""
    delta = {"pass_rate_programmatic": delta_prog}
    b = {"run_summary": {"v1": {"pass_rate": {"mean": 0.8}}, "v2": {"pass_rate": {"mean": 0.6}}, "delta": delta}}
    if comparison_summary is not None:
        b["comparison_summary"] = comparison_summary
    return b


def test_parse_delta():
    assert parse_delta("+0.27") == 0.27
    assert parse_delta("-0.10") == -0.10
    assert parse_delta(None) is None
    assert parse_delta("not-a-number") is None


def test_programmatic_pass_no_regression():
    r = evaluate_gate(_bench("+0.27"))
    assert r.decision == "PASS"
    assert r.exit_code == 0
    assert r.axes["programmatic"].verdict == "PASS"
    assert r.axes["programmatic"].delta == 0.27


def test_programmatic_fail_regression():
    r = evaluate_gate(_bench("-0.10"))
    assert r.decision == "FAIL"
    assert r.exit_code == 1
    assert r.axes["programmatic"].verdict == "FAIL"


def test_programmatic_null_no_comparison_is_NA():
    """纯主观题(programmatic null)+ 未请求 comparison → 无适用轴 → N/A。"""
    r = evaluate_gate(_bench(None))
    assert r.decision == "N/A"
    assert r.exit_code == 2


def test_candidate_and_baseline_resolved_from_side_keys():
    """_resolve_sides:无 --candidate → 从 run_summary side key 取候选/基线。"""
    r = evaluate_gate(_bench("+0.27"))  # _bench 建 v1/v2 side keys,无 comparison_summary
    assert r.candidate == "v1"
    assert r.baseline == "v2"


def test_programmatic_guard_disabled_makes_axis_not_applicable():
    """--no-programmatic-guard → 客观轴不介入(applicable=False)。"""
    r = evaluate_gate(_bench("+0.27"), programmatic_guard=False)
    assert r.axes["programmatic"].applicable is False
    assert r.decision == "N/A"  # 客观关 + comparison 桩未介入 → 无适用轴
    assert r.exit_code == 2
