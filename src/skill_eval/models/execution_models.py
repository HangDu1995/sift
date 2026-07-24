"""Execution configuration and output models."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class RunConfig(BaseModel):
    runs_per_config: int = 3
    model: str | None = None
    timeout_seconds: int = 120
    max_workers: int = 4
    output_dir: Path = Path("output")
    max_turns: int = 10
    skip_calibration: bool = False
    skip_compliance: bool = False
    refine_instructions: bool = True
    eval_samples: int | float | None = None
    per_eval_timeout: int = 300  # Per-eval timeout for outlier detection
    outlier_threshold_multiplier: float = 3.0  # > N×MAD → anomaly
    min_runs_for_stats: int = 3  # Minimum runs before statistical checks kick in
    max_retries_per_eval: int = 2  # Max retries for UNEVAL/TIMEOUT


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


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str
