"""Generalization gap calculator — compares train vs holdout benchmark results."""

from __future__ import annotations

from skill_eval.models import GeneralizationGap


class GapCalculator:
    def calculate(self, train_benchmark: dict, holdout_benchmark: dict) -> GeneralizationGap:
        train_summary = train_benchmark.get("run_summary", {})
        holdout_summary = holdout_benchmark.get("run_summary", {})

        train_ws = train_summary.get("with_skill", {}).get("pass_rate", {})
        train_wos = train_summary.get("without_skill", {}).get("pass_rate", {})
        holdout_ws = holdout_summary.get("with_skill", {}).get("pass_rate", {})
        holdout_wos = holdout_summary.get("without_skill", {}).get("pass_rate", {})

        train_delta_raw = train_summary.get("delta", {}).get("pass_rate", "0")
        holdout_delta_raw = holdout_summary.get("delta", {}).get("pass_rate", "0")

        train_delta = float(train_delta_raw) if isinstance(train_delta_raw, str) and train_delta_raw else 0.0
        holdout_delta = float(holdout_delta_raw) if isinstance(holdout_delta_raw, str) and holdout_delta_raw else 0.0

        gap = train_delta - holdout_delta

        if gap < 0.1:
            verdict = "good"
        elif gap < 0.2:
            verdict = "warn"
        else:
            verdict = "overfit"

        return GeneralizationGap(
            train_pass_rate_with=train_ws.get("mean", 0.0),
            train_pass_rate_without=train_wos.get("mean", 0.0),
            train_delta=train_delta,
            holdout_pass_rate_with=holdout_ws.get("mean", 0.0),
            holdout_pass_rate_without=holdout_wos.get("mean", 0.0),
            holdout_delta=holdout_delta,
            gap=gap,
            verdict=verdict,
        )
