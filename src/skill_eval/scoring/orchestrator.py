"""Scoring orchestrator — merges programmatic and LLM results."""

from __future__ import annotations

from pathlib import Path

from skill_eval.models import (
    EvalCase,
    ExpectationResult,
    GradingResult,
    GradingSummary,
    ScoringConfig,
)
from skill_eval.scoring.programmatic import ProgrammaticChecker
from skill_eval.scoring.llm_grader import LLMGrader


class ScoringOrchestrator:
    def __init__(self, config: ScoringConfig):
        self.config = config
        self.programmatic = ProgrammaticChecker()
        self.llm_grader = LLMGrader(
            model=config.llm_model,
            timeout=config.llm_timeout,
        ) if config.enable_llm else None

    def score(self, eval_case: EvalCase, transcript: str, output_dir: Path) -> GradingResult:
        prog_results: list[ExpectationResult] = []
        llm_result: GradingResult | None = None

        if self.config.enable_programmatic:
            prog_results = self.programmatic.check(eval_case, output_dir)

        if self.config.enable_llm and self.llm_grader:
            llm_result = self.llm_grader.grade(eval_case, transcript, output_dir)

        if not prog_results and llm_result is None:
            return GradingResult(
                expectations=[],
                summary=GradingSummary(passed=0, failed=0, total=0, pass_rate=0.0),
            )

        if llm_result is None:
            return self._build_from_programmatic(prog_results, llm_result)

        if not prog_results:
            return llm_result

        return self._merge(prog_results, llm_result)

    def _build_from_programmatic(self, prog: list[ExpectationResult], llm_result: GradingResult | None) -> GradingResult:
        expectations = list(prog)
        if llm_result:
            expectations.extend(llm_result.expectations)
        passed = sum(1 for e in expectations if e.passed)
        total = len(expectations)
        return GradingResult(
            expectations=expectations,
            summary=GradingSummary(
                passed=passed,
                failed=total - passed,
                total=total,
                pass_rate=passed / total if total > 0 else 0.0,
            ),
            execution_metrics=llm_result.execution_metrics if llm_result else None,
            timing=llm_result.timing if llm_result else None,
            claims=llm_result.claims if llm_result else [],
            eval_feedback=llm_result.eval_feedback if llm_result else None,
        )

    def _merge(self, prog: list[ExpectationResult], llm_result: GradingResult) -> GradingResult:
        merged: list[ExpectationResult] = []
        llm_by_text = {e.text: e for e in llm_result.expectations}

        for pr in prog:
            if pr.text in llm_by_text:
                lr = llm_by_text[pr.text]
                if pr.passed != lr.passed:
                    merged.append(ExpectationResult(
                        text=lr.text,
                        passed=lr.passed,
                        evidence=f"[合并] 程序化={pr.passed} LLM={lr.passed}. {lr.evidence}",
                        source="merged",
                    ))
                else:
                    merged.append(ExpectationResult(
                        text=lr.text,
                        passed=lr.passed,
                        evidence=lr.evidence,
                        source="merged",
                    ))
            else:
                merged.append(pr)

        for lr in llm_result.expectations:
            if lr.text not in {e.text for e in merged}:
                merged.append(lr)

        passed = sum(1 for e in merged if e.passed)
        total = len(merged)
        return GradingResult(
            expectations=merged,
            summary=GradingSummary(
                passed=passed,
                failed=total - passed,
                total=total,
                pass_rate=passed / total if total > 0 else 0.0,
            ),
            execution_metrics=llm_result.execution_metrics,
            timing=llm_result.timing,
            claims=llm_result.claims,
            user_notes_summary=llm_result.user_notes_summary,
            eval_feedback=llm_result.eval_feedback,
        )
