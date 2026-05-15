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


class BenchmarkData(BaseModel):
    metadata: BenchmarkMetadata
    runs: list[RunResultEntry] = Field(default_factory=list)
    run_summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[Any] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
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
    agreement_rate: float
    disagreements: list[DisagreementRecord] = Field(default_factory=list)
    confidence_signals: list[ConfidenceSignal] = Field(default_factory=list)
    overall_confidence: float
    grade: ConfidenceGrade
    recommendations: list[str] = Field(default_factory=list)


class TriggerQuery(BaseModel):
    query: str
    should_trigger: bool


class LoopConfig(BaseModel):
    max_iterations: int = 5
    runs_per_query: int = 3
    trigger_threshold: float = 0.5
    holdout_fraction: float = 0.4
    seed: int = 42
    model: str | None = None
    num_workers: int = 10
    timeout: int = 30


class IterationRecord(BaseModel):
    iteration: int
    description: str
    train_passed: int
    train_total: int
    test_passed: int | None = None
    test_total: int | None = None
    results: list[Any] = Field(default_factory=list)


class LoopResult(BaseModel):
    exit_reason: str
    original_description: str
    best_description: str
    best_score: float
    best_score_label: str
    iterations_run: int
    history: list[IterationRecord] = Field(default_factory=list)


class SkillEntry(BaseModel):
    path: str
    depends_on: list[str] = Field(default_factory=list)
    evals_path: str | None = None
    disabled: bool = False


class IntegrationEval(BaseModel):
    id: int
    prompt: str
    expected_skills_triggered: list[str] = Field(default_factory=list)
    expectations: list[str] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)


class SkillsManifest(BaseModel):
    name: str
    skills: list[SkillEntry] = Field(default_factory=list)
    integration_evals: list[IntegrationEval] = Field(default_factory=list)


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


class GeneralizationGap(BaseModel):
    domain_pass_rate_with: float = 0.0
    domain_pass_rate_without: float = 0.0
    domain_delta: float = 0.0
    general_pass_rate_with: float = 0.0
    general_pass_rate_without: float = 0.0
    general_delta: float = 0.0
    gap: float = 0.0
    verdict: str = ""


class OverfittingReport(BaseModel):
    skill_name: str = ""
    generalization_gap: GeneralizationGap | None = None
    overfitting_score: float = 0.0
    recommendation: str = ""


class SourceProfile(BaseModel):
    project_path: str = ""
    file_type_distribution: dict[str, int] = Field(default_factory=dict)
    directory_structure_summary: str = ""
    domain_keywords: list[str] = Field(default_factory=list)
    sample_files: list[dict] = Field(default_factory=list)
    project_summary: str = ""
