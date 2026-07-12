"""Batch, compliance, calibration, optimization, security, and overfitting models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from skill_eval.models.analysis_models import AnalysisResult
from skill_eval.models.comparison_models import ComparisonResult
from skill_eval.models.enums import (
    CheckMethod,
    ConfidenceGrade,
    InstructionPriority,
    InstructionSource,
    ResolutionStrategy,
    SecuritySeverity,
    SecurityVerdict,
    ScannerStatus,
    SignalType,
)
from skill_eval.models.eval_models import Assertion
from skill_eval.models.benchmark_models import (
    AnomalyReport,
    CalibrationSummary,
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
    """盲比较聚合(主观侧并列主位)。win-rate 有效分母 = with+without+genuine tie;
    failed(调用/解析失败、重试耗尽)排除出分母——失败≠打平。"""
    total: int = 0
    with_wins: int = 0
    without_wins: int = 0
    ties: int = 0          # genuine tie(有效,计入分母)
    failed: int = 0        # 无信号,排除出分母
    valid: int = 0         # with_wins + without_wins + ties
    win_rate: float | None = None  # with_wins / valid;valid==0 → None

    @classmethod
    def from_comparisons(cls, comparisons: list) -> "ComparisonSummary | None":
        if not comparisons:
            return None

        def _is(c, w):
            return c.winner == w and not getattr(c, "failed", False)

        with_wins = sum(1 for c in comparisons if _is(c, "with"))
        without_wins = sum(1 for c in comparisons if _is(c, "without"))
        # genuine tie:winner 非 with/without 且非 failed(失败-tie 因 failed=True 被排除)
        ties = sum(1 for c in comparisons
                   if c.winner not in ("with", "without") and not getattr(c, "failed", False))
        failed = sum(1 for c in comparisons if getattr(c, "failed", False))
        valid = with_wins + without_wins + ties
        return cls(
            total=len(comparisons), with_wins=with_wins, without_wins=without_wins,
            ties=ties, failed=failed, valid=valid,
            win_rate=with_wins / valid if valid else None,
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


class ComplianceResultItem(BaseModel):
    instruction: InstructionCheck
    followed: bool
    evidence: str
    confidence: float
    source: str


class ComplianceReport(BaseModel):
    skill_name: str
    total_instructions: int
    followed_count: int
    compliance_rate: float
    checks: list[ComplianceResultItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    filtered_count: int = 0  # P0-1b: CONSTRAINT 候选经 LLM 精筛剔除的非可遵循项(路由提示/描述语)
    # P0-1e: 拆双率。compliance_rate 是总率(混算,历史兼容);这两条按检查来源拆 ——
    # programmatic(TOOL_CALL_CHECK/PATTERN_MATCH/FILE_PRESENCE)稳、可信;
    # llm(LLM_JUDGE)漂(§41 探针:无 artifact 的模糊项 23% 真方差),只看方向。
    # None = 该类无指令(不报假 0)。让可信信号不再被 LLM 噪声污染。
    compliance_rate_programmatic: float | None = None
    compliance_rate_llm: float | None = None


class DisagreementRecord(BaseModel):
    expectation_text: str
    programmatic_verdict: bool | None = None
    llm_verdict: bool | None = None
    resolution: ResolutionStrategy
    evidence_analysis: str


class ConfidenceSignal(BaseModel):
    signal_type: SignalType
    description: str
    impact: float


class CalibrationReport(BaseModel):
    total_expectations: int
    programmatic_only_count: int
    llm_only_count: int
    both_evaluated: int
    agreement_count: int
    agreement_rate: float | None  # None = 无 prog/llm 双判数据(P2-3),非"0% 一致"
    disagreements: list[DisagreementRecord] = Field(default_factory=list)
    confidence_signals: list[ConfidenceSignal] = Field(default_factory=list)
    overall_confidence: float
    grade: ConfidenceGrade
    recommendations: list[str] = Field(default_factory=list)


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
