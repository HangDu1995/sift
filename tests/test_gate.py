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


def _cs(a_wins, b_wins, ties=0, label_a="v1", label_b="v2"):
    return {"a_wins": a_wins, "b_wins": b_wins, "ties": ties, "failed": 0,
            "valid": a_wins + b_wins + ties, "win_rate": None,
            "label_a": label_a, "label_b": label_b}


def test_comparison_pass():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)), min_win_rate=0.5, min_valid=3)
    assert r.decision == "PASS"
    cv = r.axes["comparison"]
    assert cv.verdict == "PASS" and cv.win_rate == 0.75 and cv.decisive == 8


def test_comparison_boundary_at_threshold_is_pass():
    """wr == min_win_rate → PASS(>= 语义:达标即过,边界不算失败)。"""
    r = evaluate_gate(_bench("+0.27", _cs(3, 3)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "PASS"
    assert r.axes["comparison"].win_rate == 0.5


def test_comparison_fail_below_threshold():
    r = evaluate_gate(_bench("+0.27", _cs(2, 6)), min_win_rate=0.5, min_valid=3)
    assert r.decision == "FAIL"  # 主观不过 → 整体 FAIL(客观虽 PASS)
    assert r.axes["comparison"].verdict == "FAIL"


def test_comparison_NA_insufficient_sample():
    r = evaluate_gate(_bench("+0.27", _cs(2, 0)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "N/A"  # decisive 2 < min_valid 3
    assert r.decision == "N/A"  # 客观 PASS 但主观样本不足 → N/A


def test_comparison_NA_all_ties():
    r = evaluate_gate(_bench("+0.27", _cs(0, 0, ties=5)), min_win_rate=0.5, min_valid=3)
    assert r.axes["comparison"].verdict == "N/A"  # decisive 0


def test_comparison_not_requested_is_not_applicable():
    """未给 --min-win-rate → comparison 不介入,只走客观。"""
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)))
    assert r.axes["comparison"].applicable is False
    assert r.decision == "PASS"  # 仅客观


def test_comparison_requested_but_summary_missing_warns():
    """请求了主观 gate 但 comparison_summary 缺失 → 不介入 + 警告;落回客观。"""
    r = evaluate_gate(_bench("+0.27"), min_win_rate=0.5)  # 无 comparison_summary
    assert r.axes["comparison"].applicable is False
    assert len(r.warnings) == 1
    assert r.decision == "PASS"  # 仅客观判


def test_programmatic_fail_overrides_comparison_pass():
    """客观回归压倒主观胜率:delta<0 直接 FAIL。"""
    r = evaluate_gate(_bench("-0.10", _cs(6, 2)), min_win_rate=0.5)
    assert r.decision == "FAIL" and r.exit_code == 1


def test_no_programmatic_guard_only_comparison():
    r = evaluate_gate(_bench("-0.10", _cs(6, 2)),
                      min_win_rate=0.5, programmatic_guard=False)
    assert r.axes["programmatic"].applicable is False
    assert r.decision == "PASS"  # 只看主观,客观回归被关


def test_no_engaged_axis_is_NA():
    """客观 null + 守卫关 + 无主观请求 → 无适用轴 → N/A。"""
    r = evaluate_gate(_bench(None), programmatic_guard=False)
    assert r.decision == "N/A" and r.exit_code == 2


def test_candidate_override():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2, label_a="v1", label_b="v2")),
                      min_win_rate=0.5, candidate="custom-cand")
    assert r.candidate == "custom-cand"
    assert r.baseline == "v2"  # label_b 仍取 comparison_summary


def test_candidate_resolution_from_side_keys():
    """无 comparison_summary → 候选/基线从 run_summary side key 取。"""
    r = evaluate_gate(_bench("+0.27"))  # 无 cs
    assert r.candidate == "v1" and r.baseline == "v2"


def test_thresholds_recorded():
    r = evaluate_gate(_bench("+0.27", _cs(6, 2)), min_win_rate=0.6, min_valid=5)
    assert r.thresholds == {"min_win_rate": 0.6, "min_valid": 5, "programmatic_guard": True}
