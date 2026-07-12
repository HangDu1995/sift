"""gate 判决纯函数(块 C)。读 benchmark dict → GateResult,无 IO。"""
from __future__ import annotations
from .models import AxisVerdict, GateResult


def parse_delta(value) -> float | None:
    """"+0.27" → 0.27;None/无法解析 → None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_sides(benchmark: dict, candidate: str | None) -> tuple[str, str]:
    """候选/基线解析:--candidate > comparison_summary.label_a > 首个 side key。"""
    run_summary = benchmark.get("run_summary", {}) or {}
    side_keys = [k for k in run_summary if k != "delta"]
    cs = benchmark.get("comparison_summary") or {}
    cand = candidate or cs.get("label_a") or (side_keys[0] if side_keys else "")
    base = cs.get("label_b") or (side_keys[1] if len(side_keys) > 1 else "")
    return cand, base


def _eval_programmatic(benchmark: dict, guard_on: bool) -> AxisVerdict:
    if not guard_on:
        return AxisVerdict(verdict="N/A", applicable=False,
                           reason="客观守卫已关(--no-programmatic-guard)")
    raw = (benchmark.get("run_summary", {}) or {}).get("delta", {}).get("pass_rate_programmatic")
    delta = parse_delta(raw)
    if delta is None:
        return AxisVerdict(verdict="N/A", applicable=False,
                           reason="delta.pass_rate_programmatic 无值(纯主观题 / 无 programmatic expectation)")
    if delta < 0:
        return AxisVerdict(verdict="FAIL", delta=delta, reason=f"客观侧回归({delta:+.2f})")
    return AxisVerdict(verdict="PASS", delta=delta, reason=f"客观侧无回归({delta:+.2f})")


def _eval_comparison(benchmark: dict, min_win_rate: float | None, min_valid: int,
                     warnings: list[str]) -> AxisVerdict:
    # Task 2 实现;此处桩 → 不介入。
    return AxisVerdict(verdict="N/A", applicable=False, reason="主观 gate 未请求")


def evaluate_gate(
    benchmark: dict,
    *,
    min_win_rate: float | None = None,
    min_valid: int = 3,
    programmatic_guard: bool = True,
    candidate: str | None = None,
) -> GateResult:
    warnings: list[str] = []
    p = _eval_programmatic(benchmark, programmatic_guard)
    c = _eval_comparison(benchmark, min_win_rate, min_valid, warnings)
    axes = {"programmatic": p, "comparison": c}

    engaged = [v for v in axes.values() if v.applicable]
    if not engaged:
        decision, exit_code = "N/A", 2
    elif any(v.verdict == "FAIL" for v in engaged):
        decision, exit_code = "FAIL", 1
    elif any(v.verdict == "N/A" for v in engaged):
        decision, exit_code = "N/A", 2
    else:
        decision, exit_code = "PASS", 0
    reason = "; ".join(v.reason for v in engaged) if engaged else "无适用轴可判"

    cand, base = _resolve_sides(benchmark, candidate)
    return GateResult(
        decision=decision, exit_code=exit_code, reason=reason,
        candidate=cand, baseline=base,
        thresholds={"min_win_rate": min_win_rate, "min_valid": min_valid,
                    "programmatic_guard": programmatic_guard},
        axes=axes, warnings=warnings,
    )
