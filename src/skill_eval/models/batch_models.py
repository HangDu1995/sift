"""Batch, compliance, calibration, optimization, security, and overfitting models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from skill_eval.models.analysis_models import AnalysisResult
from skill_eval.models.comparison_models import ComparisonResult
from skill_eval.models.enums import (
    CheckMethod,
    CalibrationVerdict,
    InstructionPriority,
    InstructionSource,
    ProcessStatus,
    SecuritySeverity,
    SecurityVerdict,
    ScannerStatus,
)
from skill_eval.models.eval_models import Assertion
from skill_eval.models.benchmark_models import (
    AnomalyReport,
    ComplianceSummary,
    RunResultEntry,
    StatsValue,
)


class BenchmarkMetadata(BaseModel):
    skill_name: str
    skill_path: str = ""
    executor_model: str = ""
    analyzer_model: str = ""
    comparator_model: str = ""
    timestamp: str = ""
    evals_run: list[int] = Field(default_factory=list)
    runs_per_configuration: int = 3


class ComparisonSummary(BaseModel):
    """盲比较聚合(主观侧并列主位)。win-rate 有效分母 = a+b+genuine tie;
    failed(调用/解析失败、重试耗尽)排除出分母——失败≠打平。

    a/b = side A/B。label_a/b 默认 "with"/"without"(with/without 模式);
    v1/v2 模式传 v1/v2。win_rate 恒为 side A 胜率(a_wins/valid)。"""
    total: int = 0
    a_wins: int = 0
    b_wins: int = 0
    ties: int = 0          # genuine tie(有效,计入分母)
    failed: int = 0        # 无信号,排除出分母
    valid: int = 0         # a_wins + b_wins + ties
    win_rate: float | None = None  # a_wins / valid;valid==0 → None
    label_a: str = "with"
    label_b: str = "without"

    @classmethod
    def from_comparisons(
        cls,
        comparisons: list,
        label_a: str = "with",
        label_b: str = "without",
    ) -> "ComparisonSummary | None":
        if not comparisons:
            return None

        def _is(c, w):
            return c.winner == w and not getattr(c, "failed", False)

        a_wins = sum(1 for c in comparisons if _is(c, label_a))
        b_wins = sum(1 for c in comparisons if _is(c, label_b))
        # genuine tie:winner 非 label_a/b 且非 failed(失败-tie 因 failed=True 被排除)
        ties = sum(1 for c in comparisons
                   if c.winner not in (label_a, label_b) and not getattr(c, "failed", False))
        failed = sum(1 for c in comparisons if getattr(c, "failed", False))
        valid = a_wins + b_wins + ties
        return cls(
            total=len(comparisons), a_wins=a_wins, b_wins=b_wins,
            ties=ties, failed=failed, valid=valid,
            win_rate=a_wins / valid if valid else None,
            label_a=label_a, label_b=label_b,
        )


class BenchmarkData(BaseModel):
    metadata: BenchmarkMetadata
    runs: list[RunResultEntry] = Field(default_factory=list)
    run_summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[Any] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    comparison_summary: ComparisonSummary | None = None
    analysis: AnalysisResult | None = None
    anomaly_report: AnomalyReport | None = None


class InstructionCheck(BaseModel):
    source: InstructionSource
    instruction_text: str
    section: str
    priority: InstructionPriority
    check_method: CheckMethod
    params: dict[str, Any] = Field(default_factory=dict)


class HardViolation(BaseModel):
    """硬类(prohibition/output)违规明细,供人秒辨真伪。"""
    category: str          # AuditCategory 值:"prohibition" | "output"
    instruction_text: str
    evidence: str          # AuditFinding.evidence(为何 FAIL,中文)


class CalibrationReport(BaseModel):
    """单次 with_skill 执行的过程 x 结果 coherence 判决。
    gate 不读本结构(只 viewer 显示用)。verdict 由结果(pass_rate>=0.8) x 过程(硬类违规)矩阵决定。
    """
    verdict: CalibrationVerdict
    result_pass: bool
    process_status: ProcessStatus
    hard_violations: list[HardViolation] = Field(default_factory=list)
    soft_notes: dict[str, int] = Field(default_factory=dict)  # {"step": N, "conditional": N} = FAIL 计数
    scoring_rate: float | None     # = grading.summary.pass_rate(显示用)
    compliance_rate: float | None  # = pass/(pass+fail)(显示用);过程未知时 None


class SkillEntry(BaseModel):
    path: str
    depends_on: list[str] = Field(default_factory=list)
    evals_path: str | None = None
    disabled: bool = False


class SkillsManifest(BaseModel):
    name: str
    skills: list[SkillEntry] = Field(default_factory=list)


class SecurityFinding(BaseModel):
    scanner: str
    severity: SecuritySeverity
    description: str
    file_path: str = ""
    line: int | None = None


class ScannerResult(BaseModel):
    name: str
    status: ScannerStatus
    findings: list[SecurityFinding] = Field(default_factory=list)
    message: str = ""


class SecurityReport(BaseModel):
    skill_name: str
    skill_path: str
    verdict: SecurityVerdict
    scanners: list[ScannerResult] = Field(default_factory=list)
    findings: list[SecurityFinding] = Field(default_factory=list)
    summary: str = ""
