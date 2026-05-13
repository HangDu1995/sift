"""Generalization gap calculator — compares domain vs general benchmark results."""

from __future__ import annotations

from skill_eval.models import GeneralizationGap


class GapCalculator:
    def calculate(self, domain_benchmark: dict, general_benchmark: dict) -> GeneralizationGap:
        domain_summary = domain_benchmark.get("run_summary", {})
        general_summary = general_benchmark.get("run_summary", {})

        domain_ws = domain_summary.get("with_skill", {}).get("pass_rate", {})
        domain_wos = domain_summary.get("without_skill", {}).get("pass_rate", {})
        general_ws = general_summary.get("with_skill", {}).get("pass_rate", {})
        general_wos = general_summary.get("without_skill", {}).get("pass_rate", {})

        domain_delta_raw = domain_summary.get("delta", {}).get("pass_rate", "0")
        general_delta_raw = general_summary.get("delta", {}).get("pass_rate", "0")

        domain_delta = float(domain_delta_raw) if isinstance(domain_delta_raw, str) and domain_delta_raw else 0.0
        general_delta = float(general_delta_raw) if isinstance(general_delta_raw, str) and general_delta_raw else 0.0

        gap = domain_delta - general_delta

        if gap < 0.1:
            verdict = "good"
        elif gap < 0.2:
            verdict = "warn"
        else:
            verdict = "overfit"

        return GeneralizationGap(
            domain_pass_rate_with=domain_ws.get("mean", 0.0),
            domain_pass_rate_without=domain_wos.get("mean", 0.0),
            domain_delta=domain_delta,
            general_pass_rate_with=general_ws.get("mean", 0.0),
            general_pass_rate_without=general_wos.get("mean", 0.0),
            general_delta=general_delta,
            gap=gap,
            verdict=verdict,
        )
