"""Benchmark, static analysis, and run result models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from skill_eval.models.enums import ConfidenceGrade, SkillCategory
from skill_eval.models.execution_models import CheckResult
from skill_eval.models.grading_models import ExpectationResult


class AnalysisReport(BaseModel):
    skill_name: str
    skill_path: str
    category: SkillCategory
    checks: list[CheckResult] = Field(default_factory=list)
    score: int = 0
    max_score: int = 0
    timestamp: str = ""

    def model_post_init(self, __context) -> None:
        if self.max_score == 0 and self.checks:
            self.max_score = len(self.checks)


class RunResultMetrics(BaseModel):
    pass_rate: float
    passed: int
    failed: int
    total: int
    time_seconds: float = 0.0
    tokens: int = 0
    tool_calls: int = 0
    errors: int = 0


class ComplianceSummary(BaseModel):
    compliance_rate: float
    followed_count: int
    total_instructions: int
    required_followed: int
    required_total: int


class CalibrationSummary(BaseModel):
    grade: ConfidenceGrade
    overall_confidence: float
    agreement_rate: float | None  # None = 无 prog/llm 双判数据(P2-3);死字段(当前无构造点),随 CalibrationReport 同步
    disagreement_count: int


class RunResultEntry(BaseModel):
    eval_id: int
    eval_name: str = ""
    configuration: str
    run_number: int
    result: RunResultMetrics
    expectations: list[ExpectationResult] = Field(default_factory=list)
    compliance: ComplianceSummary | None = None
    calibration: CalibrationSummary | None = None
    notes: list[str] = Field(default_factory=list)
    is_anomaly: bool = False
    anomaly_reason: str = ""
    retry_count: int = 0


class RunAnomaly(BaseModel):
    eval_id: int
    eval_name: str
    configuration: str
    run_number: int
    anomaly_type: str  # "timeout", "outlier", "uneval", "error", "retry_exhausted"
    detail: str
    value: float = 0.0
    threshold: float = 0.0


class AnomalyReport(BaseModel):
    anomalies: list[RunAnomaly] = Field(default_factory=list)
    total_runs: int = 0
    anomaly_count: int = 0
    retried_count: int = 0
    unresolved_count: int = 0


class StatsValue(BaseModel):
    mean: float
    stddev: float
    min: float
    max: float


class RunSummaryConfig(BaseModel):
    pass_rate: StatsValue
    time_seconds: StatsValue
    tokens: StatsValue
