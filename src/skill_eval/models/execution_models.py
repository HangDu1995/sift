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
    generate_headline: bool = True
    eval_samples: int | float | None = None
    eval_ids: list[int] | None = None  # 按 id 指定某几道题(优先于 eval_samples;互斥)
    per_eval_timeout: int = 300  # Per-eval timeout for outlier detection
    outlier_threshold_multiplier: float = 3.0  # > N×MAD → anomaly
    min_runs_for_stats: int = 3  # Minimum runs before statistical checks kick in
    max_retries_per_eval: int = 2  # Max retries for UNEVAL/TIMEOUT
    kind: str = "skill"  # 声明类型/注入模式:skill(skill body 拼 user prompt)/ agent(--append-system-prompt 注入 system 层,单臂绝对质量)


class ToolCallRecord(BaseModel):
    name: str
    input_summary: str = ""  # json.dumps(input)[:200],显示用(报告/prompt);匹配优先用 input
    input: dict | None = None  # 完整结构化参数(不截断),供参数级匹配/路径规范化;None=无结构化数据
    turn: int | None = None  # 该 tool_use 所属的全局 turnIndex(None=未标注)


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
