"""Grading, timing, and metrics models."""

from __future__ import annotations

from pydantic import BaseModel, Field

from skill_eval.models.enums import AssertionVerdict


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
    verdict: AssertionVerdict = AssertionVerdict.PASS
    retry_count: int = 0


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
    error: str = ""
