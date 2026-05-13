"""Overfitting detector — builds report from generalization gap statistics."""

from __future__ import annotations

from skill_eval.models import GeneralizationGap, OverfittingReport


class OverfittingDetector:
    def __init__(self):
        pass

    def build_report(
        self,
        skill_name: str = "",
        gap_data: dict | None = None,
    ) -> OverfittingReport:
        gap = None
        if gap_data:
            gap = GeneralizationGap(**gap_data)

        if gap:
            score = {"good": 0.1, "warn": 0.4, "overfit": 0.7}.get(gap.verdict, 0.1)
        else:
            score = 0.0

        if gap and gap.verdict == "overfit":
            recommendation = (
                f"Generalization gap is large ({gap.gap:.2f}). "
                "The skill performs much better on domain evals than general evals. "
                "Stop optimizing against the current project data."
            )
        elif gap and gap.verdict == "warn":
            recommendation = (
                f"Moderate generalization gap ({gap.gap:.2f}). "
                "The skill may be starting to overfit to the domain. "
                "Review skill modifications for domain-specific rules."
            )
        elif gap:
            recommendation = "No overfitting detected. Generalization gap is within acceptable range."
        else:
            recommendation = "Insufficient data for overfitting assessment."

        return OverfittingReport(
            skill_name=skill_name,
            generalization_gap=gap,
            overfitting_score=score,
            recommendation=recommendation,
        )
