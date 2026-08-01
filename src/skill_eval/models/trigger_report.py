"""trigger-accuracy 评测报告模型(写 trigger-report.json)。"""

from pydantic import BaseModel, Field


class Suggestion(BaseModel):
    """给用户的操作建议(怎么改 description 才更容易 / 更不容易被触发)。"""
    category: str               # recall / false_trigger / unresolved / all_good / llm_rewrite
    source: str                 # rule(规则,总在)/ llm(有失败时自动调 LLM 起草)
    message: str                # 中文、可直接照做的建议文案
    probe_ids: list[int] = []   # 关联的 probe id(哪些 probe 触发了这条建议)


class ProbeResult(BaseModel):
    """单条 trigger 探题的结果。"""
    id: int
    expect_trigger: bool
    prompt: str = ""            # 该 probe 的中性请求原文(建议文案引用 + HTML 展示)
    trigger_rate: float | None  # 跨 N runs 触发率(fired 次 / runs);UNRESOLVED(没判成)→ None
    verdict: str                # PASS / FAIL / UNRESOLVED
    transcript_path: str = ""   # 首个成功 run 的 transcript(人审/重跑抓手)


class TriggerReport(BaseModel):
    """trigger-accuracy 报告:recall(false-negative)+ false-trigger-rate(false-positive)。"""
    skill_name: str
    description: str = ""       # 被测 skill 的 frontmatter description 原文(HTML 展示 + LLM 建议输入)
    description_hash: str       # sha256(frontmatter description)[:16] —— 触发判定唯一输入的锚
    runs: int
    recall: float | None        # should-trigger 方向均值;该方向空 → None(N/A)
    false_trigger_rate: float | None  # should-not 方向均值;该方向空 → None(N/A)
    n_should_trigger: int
    n_should_not: int
    unresolved: int = 0          # spawn 异常 / cc 非零退出没触发 的 probe 数(不进分母)
    token_usage: dict = {}       # 合计 token(各 probe cc spawn + LLM 建议)
    duration_seconds: float = 0.0  # cc spawn 总墙钟(各 probe 各 run RunOutput.duration_ms 之和 / 1000)
    suggestions: list[Suggestion] = Field(default_factory=list)  # 操作建议(规则 + 可选 LLM)
    probes: list[ProbeResult]
