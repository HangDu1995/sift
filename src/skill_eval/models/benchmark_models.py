"""Benchmark, static analysis, and run result models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from skill_eval.models.enums import SkillCategory
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
    # 双率(与 grading summary 同源):per-(eval,run) 的程序/LLM 分项通过率,透到 viewer
    # 做检查点总览 + 逐项表的 3-dim 列。None = 该 run 无该类 expectation(诚实缺值)。
    pass_rate_programmatic: float | None = None
    pass_rate_llm: float | None = None


class ComplianceSummary(BaseModel):
    compliance_rate: float
    followed_count: int
    total_instructions: int
    required_followed: int
    required_total: int


class RunResultEntry(BaseModel):
    eval_id: int
    eval_name: str = ""
    configuration: str
    run_number: int
    result: RunResultMetrics
    expectations: list[ExpectationResult] = Field(default_factory=list)
    compliance: ComplianceSummary | None = None
    notes: list[str] = Field(default_factory=list)
    is_anomaly: bool = False
    anomaly_reason: str = ""
    retry_count: int = 0
    # 失败三角化(纯诊断,不进判决):从 grading.error 抽的根因 + 修复建议。None = 该 run 无 error。
    error_root_cause: str | None = None
    error_suggestion: str | None = None


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
