"""Calibration checker — 单次执行的过程 x 结果 coherence 判决。

verdict 由结果(客观 pass_rate>=0.8 优先,无客观锚回退 mixed) x 过程(遵循性)矩阵决定,只押在 prohibition/output
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
        summary = grading.summary
        prog_rate = summary.pass_rate_programmatic
        llm_rate = summary.pass_rate_llm
        # 结果轴:优先押客观 programmatic(稳、避 LLM 方差);纯主观题无客观锚 → 回退 mixed(含 LLM)。
        if prog_rate is not None:
            scoring_rate, basis = prog_rate, "programmatic"
        else:
            scoring_rate, basis = summary.pass_rate, "mixed"
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
            scoring_rate_basis=basis,
            scoring_rate_programmatic=round(prog_rate, 4) if prog_rate is not None else None,
            scoring_rate_llm=round(llm_rate, 4) if llm_rate is not None else None,
            compliance_rate=compliance_rate,
        )
