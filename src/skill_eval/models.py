"""Pydantic data models for skill-eval."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AssertionType(str, Enum):
    FILE_EXISTS = "FILE_EXISTS"
    CONTAINS_PATTERN = "CONTAINS_PATTERN"
    JSON_HAS_FIELD = "JSON_HAS_FIELD"
    LINE_COUNT = "LINE_COUNT"
    NOT_CONTAINS = "NOT_CONTAINS"
    CUSTOM_SCRIPT = "CUSTOM_SCRIPT"


class InstructionSource(str, Enum):
    HEADING_STEP = "HEADING_STEP"
    TOOL_REQUIREMENT = "TOOL_REQUIREMENT"
    CONSTRAINT = "CONSTRAINT"
    ANTI_FABRICATION = "ANTI_FABRICATION"
    OUTPUT_FORMAT = "OUTPUT_FORMAT"
    REFERENCE_USAGE = "REFERENCE_USAGE"


class InstructionPriority(str, Enum):
    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"


class CheckMethod(str, Enum):
    PATTERN_MATCH = "PATTERN_MATCH"
    TOOL_CALL_CHECK = "TOOL_CALL_CHECK"
    FILE_PRESENCE = "FILE_PRESENCE"
    LLM_JUDGE = "LLM_JUDGE"


class ConfidenceGrade(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNRELIABLE = "UNRELIABLE"


class SignalType(str, Enum):
    HIGH_AGREEMENT_RATE = "HIGH_AGREEMENT_RATE"
    LOW_AGREEMENT_RATE = "LOW_AGREEMENT_RATE"
    COMPLIANCE_BACKS_SCORING = "COMPLIANCE_BACKS_SCORING"
    COMPLIANCE_CONTRADICTS_SCORING = "COMPLIANCE_CONTRADICTS_SCORING"
    WEAK_EVIDENCE = "WEAK_EVIDENCE"
    GRADER_SKIPPED_CHECKS = "GRADER_SKIPPED_CHECKS"
    SUPERFICIAL_PASS = "SUPERFICIAL_PASS"


class ResolutionStrategy(str, Enum):
    PROGRAMMATIC_CORRECT = "PROGRAMMATIC_CORRECT"
    LLM_CORRECT = "LLM_CORRECT"
    AMBIGUOUS = "AMBIGUOUS"
    BOTH_WRONG = "BOTH_WRONG"


class SkillCategory(str, Enum):
    CAPABILITY_UPLIFT = "CAPABILITY_UPLIFT"
    ENCODED_PREFERENCE = "ENCODED_PREFERENCE"


# ---------------------------------------------------------------------------
# Eval definition
# ---------------------------------------------------------------------------


class Assertion(BaseModel):
    name: str
    type: AssertionType
    params: dict[str, Any] = Field(default_factory=dict)


class FixtureFile(BaseModel):
    path: str
    content: str = ""


class EvalCase(BaseModel):
    id: int
    name: str = ""
    prompt: str
    expected_output: str = ""
    files: list[str] = Field(default_factory=list)
    expectations: list[str] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    fixtures: list[FixtureFile] = Field(default_factory=list)
    level: str = "L1"

    def get_name(self) -> str:
        return self.name or f"eval-{self.id}"

    def setup_fixtures(self, base_dir: Path) -> None:
        for f in self.fixtures:
            target = base_dir / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")


class EvalSet(BaseModel):
    skill_name: str
    evals: list[EvalCase]


# ---------------------------------------------------------------------------
# Timing & Metrics
# ---------------------------------------------------------------------------


class TimingData(BaseModel):
    total_tokens: int = 0
    duration_ms: int = 0
    total_duration_seconds: float = 0.0


class ExecutionMetrics(BaseModel):
    tool_calls: dict[str, int] = Field(default_factory=dict)
    total_tool_calls: int = 0
    total_steps: int = 0
    files_created: list[str] = Field(default_factory=list)
    errors_encountered: int = 0
    output_chars: int = 0
    transcript_chars: int = 0


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class GradingSummary(BaseModel):
    passed: int
    failed: int
    total: int
    pass_rate: float


class ExpectationResult(BaseModel):
    text: str
    passed: bool
    evidence: str
    source: str = "llm"


class Claim(BaseModel):
    claim: str
    type: str
    verified: bool
    evidence: str


class UserNotes(BaseModel):
    uncertainties: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    workarounds: list[str] = Field(default_factory=list)


class EvalFeedbackSuggestion(BaseModel):
    assertion: str | None = None
    reason: str


class EvalFeedback(BaseModel):
    suggestions: list[EvalFeedbackSuggestion] = Field(default_factory=list)
    overall: str = ""


class GradingResult(BaseModel):
    expectations: list[ExpectationResult] = Field(default_factory=list)
    summary: GradingSummary
    execution_metrics: ExecutionMetrics | None = None
    timing: TimingData | None = None
    claims: list[Claim] = Field(default_factory=list)
    user_notes_summary: UserNotes | None = None
    eval_feedback: EvalFeedback | None = None


# ---------------------------------------------------------------------------
# Execution config
# ---------------------------------------------------------------------------


class RunConfig(BaseModel):
    runs_per_config: int = 3
    model: str | None = None
    timeout_seconds: int = 120
    max_workers: int = 4
    output_dir: Path = Path("output")
    max_turns: int = 10


class ToolCallRecord(BaseModel):
    name: str
    input_summary: str = ""


class RunOutput(BaseModel):
    exit_code: int
    stdout: str
    duration_ms: int = 0
    total_tokens: int = 0
    output_files: list[Path] = Field(default_factory=list)
    transcript: str = ""
    tool_calls: dict[str, int] = Field(default_factory=dict)
    tool_trace: list[ToolCallRecord] = Field(default_factory=list)
    result_text: str = ""
    num_turns: int = 0
    stop_reason: str = ""


class ScoringConfig(BaseModel):
    enable_programmatic: bool = True
    enable_llm: bool = True
    llm_model: str | None = None
    llm_timeout: int = 120


# ---------------------------------------------------------------------------
# Static analysis
# ---------------------------------------------------------------------------


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str


class AnalysisReport(BaseModel):
    skill_name: str
    skill_path: str
    category: SkillCategory
    checks: list[CheckResult] = Field(default_factory=list)
    score: int = 0
    max_score: int = 11
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


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
    agreement_rate: float
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


class StatsValue(BaseModel):
    mean: float
    stddev: float
    min: float
    max: float


class RunSummaryConfig(BaseModel):
    pass_rate: StatsValue
    time_seconds: StatsValue
    tokens: StatsValue


class BenchmarkMetadata(BaseModel):
    skill_name: str
    skill_path: str = ""
    executor_model: str = ""
    analyzer_model: str = ""
    timestamp: str = ""
    evals_run: list[int] = Field(default_factory=list)
    runs_per_configuration: int = 3


class BenchmarkData(BaseModel):
    metadata: BenchmarkMetadata
    runs: list[RunResultEntry] = Field(default_factory=list)
    run_summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


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


class FailureType(str, Enum):
    ROOT_CAUSE = "ROOT_CAUSE"
    CASCADE = "CASCADE"
    INDEPENDENT = "INDEPENDENT"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CASCADE_FAILED = "CASCADE_FAILED"


class EdgeType(str, Enum):
    STATIC_DEP = "STATIC_DEP"
    RUNTIME_CALL = "RUNTIME_CALL"
    MANIFEST_DECLARED = "MANIFEST_DECLARED"
