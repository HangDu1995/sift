"""Calibration checker — meta-evaluation of scoring trustworthiness."""

from __future__ import annotations

from skill_eval.models import (
    CalibrationReport,
    ComplianceReport,
    ConfidenceGrade,
    ConfidenceSignal,
    DisagreementRecord,
    ExpectationResult,
    GradingResult,
    ResolutionStrategy,
    SignalType,
)


class CalibrationChecker:
    def calibrate(
        self,
        grading: GradingResult,
        compliance: ComplianceReport | None = None,
    ) -> CalibrationReport:
        expectations = grading.expectations

        prog_results = [e for e in expectations if e.source in ("programmatic", "merged")]
        llm_results = [e for e in expectations if e.source in ("llm", "llm_batch", "merged")]

        prog_texts = {e.text for e in prog_results}
        llm_texts = {e.text for e in llm_results}
        both_texts = prog_texts & llm_texts

        programmatic_only = len(prog_texts - llm_texts)
        llm_only = len(llm_texts - prog_texts)
        both_evaluated = len(both_texts)

        disagreements = self._find_disagreements(expectations)
        agreement_count = both_evaluated - len(disagreements)
        agreement_rate = agreement_count / both_evaluated if both_evaluated > 0 else 1.0

        signals = self._detect_signals(
            agreement_rate=agreement_rate,
            both_evaluated=both_evaluated,
            total=len(expectations),
            programmatic_only=programmatic_only,
            llm_only=llm_only,
            compliance=compliance,
            expectations=expectations,
        )

        confidence = self._compute_confidence(signals)
        grade = self._grade(confidence)
        recommendations = self._generate_recommendations(signals, disagreements)

        return CalibrationReport(
            total_expectations=len(expectations),
            programmatic_only_count=programmatic_only,
            llm_only_count=llm_only,
            both_evaluated=both_evaluated,
            agreement_count=agreement_count,
            agreement_rate=round(agreement_rate, 4),
            disagreements=disagreements,
            confidence_signals=signals,
            overall_confidence=round(confidence, 4),
            grade=grade,
            recommendations=recommendations,
        )

    def _find_disagreements(self, expectations: list[ExpectationResult]) -> list[DisagreementRecord]:
        by_text: dict[str, list[ExpectationResult]] = {}
        for e in expectations:
            by_text.setdefault(e.text, []).append(e)

        disagreements: list[DisagreementRecord] = []
        for text, results in by_text.items():
            sources = {r.source for r in results}
            if "merged" in sources or len(sources) < 2:
                continue
            verdicts = {r.source: r.passed for r in results}
            prog = verdicts.get("programmatic")
            llm = verdicts.get("llm") if "llm" in verdicts else verdicts.get("llm_batch")
            if prog is not None and llm is not None and prog != llm:
                resolution = ResolutionStrategy.LLM_CORRECT if llm else ResolutionStrategy.PROGRAMMATIC_CORRECT
                disagreements.append(DisagreementRecord(
                    expectation_text=text,
                    programmatic_verdict=prog,
                    llm_verdict=llm,
                    resolution=resolution,
                    evidence_analysis=f"prog={prog} vs llm={llm}",
                ))
        return disagreements

    def _detect_signals(
        self,
        agreement_rate: float,
        both_evaluated: int,
        total: int,
        programmatic_only: int,
        llm_only: int,
        compliance: ComplianceReport | None,
        expectations: list[ExpectationResult],
    ) -> list[ConfidenceSignal]:
        signals: list[ConfidenceSignal] = []

        if agreement_rate >= 0.9 and both_evaluated > 0:
            signals.append(ConfidenceSignal(
                signal_type=SignalType.HIGH_AGREEMENT_RATE,
                description=f"Agreement rate {agreement_rate:.0%}",
                impact=0.1,
            ))
        elif agreement_rate < 0.6 and both_evaluated > 0:
            signals.append(ConfidenceSignal(
                signal_type=SignalType.LOW_AGREEMENT_RATE,
                description=f"Low agreement rate {agreement_rate:.0%}",
                impact=-0.2,
            ))

        if compliance:
            if compliance.compliance_rate >= 0.8:
                signals.append(ConfidenceSignal(
                    signal_type=SignalType.COMPLIANCE_BACKS_SCORING,
                    description=f"Compliance rate {compliance.compliance_rate:.0%}",
                    impact=0.1,
                ))
            elif compliance.compliance_rate < 0.5:
                signals.append(ConfidenceSignal(
                    signal_type=SignalType.COMPLIANCE_CONTRADICTS_SCORING,
                    description=f"Low compliance {compliance.compliance_rate:.0%}",
                    impact=-0.3,
                ))

        weak = sum(1 for e in expectations if len(e.evidence) < 20)
        if weak > total * 0.5:
            signals.append(ConfidenceSignal(
                signal_type=SignalType.WEAK_EVIDENCE,
                description=f"{weak}/{total} expectations have weak evidence",
                impact=-0.1,
            ))

        single_source = programmatic_only + llm_only
        if single_source > total * 0.5:
            signals.append(ConfidenceSignal(
                signal_type=SignalType.GRADER_SKIPPED_CHECKS,
                description=f"{single_source}/{total} single-source evaluations",
                impact=-0.1,
            ))

        return signals

    def _compute_confidence(self, signals: list[ConfidenceSignal]) -> float:
        base = 0.7
        total = base + sum(s.impact for s in signals)
        return max(0.0, min(1.0, total))

    def _grade(self, confidence: float) -> ConfidenceGrade:
        if confidence >= 0.8:
            return ConfidenceGrade.HIGH
        if confidence >= 0.6:
            return ConfidenceGrade.MEDIUM
        if confidence >= 0.4:
            return ConfidenceGrade.LOW
        return ConfidenceGrade.UNRELIABLE

    def _generate_recommendations(
        self, signals: list[ConfidenceSignal], disagreements: list[DisagreementRecord]
    ) -> list[str]:
        recs: list[str] = []
        signal_types = {s.signal_type for s in signals}

        if SignalType.LOW_AGREEMENT_RATE in signal_types:
            recs.append("Low agreement rate — consider increasing runs_per_config for statistical stability")

        if SignalType.COMPLIANCE_CONTRADICTS_SCORING in signal_types:
            recs.append("Compliance contradicts scoring — output correctness may not be due to Skill")

        if SignalType.WEAK_EVIDENCE in signal_types:
            recs.append("Weak evidence in grading — consider adding more specific expectations")

        if SignalType.GRADER_SKIPPED_CHECKS in signal_types:
            recs.append("Many single-source evaluations — enable both programmatic and LLM scoring")

        for d in disagreements[:3]:
            recs.append(f"Disagreement on '{d.expectation_text[:50]}...': resolved as {d.resolution.value}")

        return recs
