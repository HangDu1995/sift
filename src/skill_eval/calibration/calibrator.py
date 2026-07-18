"""Calibration checker — 单次执行的过程 x 结果 coherence 判决。

verdict 由结果(pass_rate>=0.8) x 过程(遵循性)矩阵决定,只押在 prohibition/output
硬类违规(高精度)上;step/conditional 的 FAIL 只记 soft_notes,不驱动 verdict(避免
LLM 方差污染判决)。gate 不读本结构,仅 viewer 显示用。
"""

from __future__ import annotations

from skill_eval.models import (
    AuditCategory,
    AuditReport,
    AuditVerdict,
    CalibrationReport,
    CalibrationVerdict,
    GradingResult,
    HardViolation,
    ProcessStatus,
)

# 结果判定阈值:与 viewer 打分 badge 绿档同值(pass_rate >= 此值 = 结果对)。
RESULT_PASS_THRESHOLD = 0.8
# 硬类:实测高精度(主动查违规 / section 限定后),驱动 verdict;软类有 LLM 方差,只记 soft_notes。
HARD_CATEGORIES = (AuditCategory.PROHIBITION, AuditCategory.OUTPUT)
SOFT_CATEGORIES = (AuditCategory.STEP, AuditCategory.CONDITIONAL)


class CalibrationChecker:
    def calibrate(
        self,
        grading: GradingResult,
        compliance: AuditReport | None = None,
    ) -> CalibrationReport:
        scoring_rate = grading.summary.pass_rate
        result_pass = scoring_rate >= RESULT_PASS_THRESHOLD

        # ---- 过程判定:只看 PASS/FAIL(排除 N/A 不适用、UNCLEAR 判不准)----
        hard_violations: list[HardViolation] = []
        soft_notes: dict[str, int] = {}
        compliance_rate: float | None = None

        judged = (
            [f for f in compliance.findings
             if f.verdict in (AuditVerdict.PASS, AuditVerdict.FAIL)]
            if compliance and compliance.findings else []
        )

        if not judged:
            process_status = ProcessStatus.UNKNOWN
        else:
            passed = sum(1 for f in judged if f.verdict == AuditVerdict.PASS)
            compliance_rate = round(passed / len(judged), 4)
            for f in judged:
                if f.verdict != AuditVerdict.FAIL:
                    continue
                if f.category in HARD_CATEGORIES:
                    hard_violations.append(HardViolation(
                        category=f.category.value,
                        instruction_text=f.instruction_text,
                        evidence=f.evidence,
                    ))
                elif f.category in SOFT_CATEGORIES:
                    soft_notes[f.category.value] = soft_notes.get(f.category.value, 0) + 1
            process_status = ProcessStatus.VIOLATED if hard_violations else ProcessStatus.OK

        # ---- 矩阵 ----
        if process_status == ProcessStatus.UNKNOWN:
            verdict = CalibrationVerdict.PROCESS_UNKNOWN
        elif process_status == ProcessStatus.OK:
            verdict = CalibrationVerdict.TRUSTED if result_pass else CalibrationVerdict.FIXABLE
        else:  # VIOLATED
            verdict = (
                CalibrationVerdict.HIGH_SCORE_SUSPECT
                if result_pass else CalibrationVerdict.SKILL_FAILED
            )

        return CalibrationReport(
            verdict=verdict,
            result_pass=result_pass,
            process_status=process_status,
            hard_violations=hard_violations,
            soft_notes=soft_notes,
            scoring_rate=round(scoring_rate, 4),
            compliance_rate=compliance_rate,
        )
