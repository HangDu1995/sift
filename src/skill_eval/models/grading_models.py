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
    # 双率:按 ExpectationResult.source 拆(与 compliance 双率 P0-1e 同构)。
    # programmatic(Tier1/2 客观)稳、可信;llm(Tier3 主观)漂,只看方向。
    # None = 该类无 expectation(不报假 0)。
    passed_programmatic: int | None = None
    total_programmatic: int | None = None
    pass_rate_programmatic: float | None = None
    passed_llm: int | None = None
    total_llm: int | None = None
    pass_rate_llm: float | None = None

    @classmethod
    def from_expectations(cls, expectations: list) -> "GradingSummary":
        """从 expectations 聚合 summary,含按 source 拆的双率。

        总 pass_rate 数全部(含 source=error 等异常项,兼容历史);
        双率只数 source=programmatic / source=llm,该类为空 → None。
        """
        total = len(expectations)
        passed = sum(1 for e in expectations if e.passed)
        prog = [e for e in expectations if getattr(e, "source", None) == "programmatic"]
        llm = [e for e in expectations if getattr(e, "source", None) == "llm"]

        def _rate(items: list) -> float | None:
            return (sum(1 for e in items if e.passed) / len(items)) if items else None

        return cls(
            passed=passed,
            failed=total - passed,
            total=total,
            pass_rate=passed / total if total > 0 else 0.0,
            passed_programmatic=sum(1 for e in prog if e.passed) if prog else None,
            total_programmatic=len(prog) if prog else None,
            pass_rate_programmatic=_rate(prog),
            passed_llm=sum(1 for e in llm if e.passed) if llm else None,
            total_llm=len(llm) if llm else None,
            pass_rate_llm=_rate(llm),
        )


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
    # 内容感知缓存键(runner 续跑用):= sha256(skill 正文 + eval 全身 + model/配置)[:16]。
    # 空 = 老 grading.json 或尚未计算 → 续跑时与任何算出的 key 不匹配 → 自动失效重跑。
    cache_key: str = ""
