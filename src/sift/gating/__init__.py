"""gate 判决包(块 C):post-hoc 对 benchmark.json 出 PASS/FAIL/N/A。"""

__all__ = ["AxisVerdict", "GateResult", "evaluate_gate", "parse_delta"]


from sift.gating.models import AxisVerdict, GateResult
from sift.gating.verdict import evaluate_gate, parse_delta
