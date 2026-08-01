"""trigger-accuracy 评测报告模型(写 trigger-report.json)。"""

from pydantic import BaseModel


class ProbeResult(BaseModel):
    """单条 trigger 探题的结果。"""
    id: int
    expect_trigger: bool
    trigger_rate: float | None   # 跨 N runs 触发率(fired 次 / runs);UNRESOLVED(没判成)→ None
    verdict: str                 # PASS / FAIL / UNRESOLVED
    transcript_path: str = ""    # 该 probe 的 transcript(人审/重跑抓手)


class TriggerReport(BaseModel):
    """trigger-accuracy 报告:recall(false-negative)+ false-trigger-rate(false-positive)。"""
    skill_name: str
    description_hash: str        # sha256(frontmatter description)[:16] —— 触发判定唯一输入的锚
    runs: int
    recall: float | None         # should-trigger 方向均值;该方向空 → None(N/A)
    false_trigger_rate: float | None  # should-not 方向均值;该方向空 → None(N/A)
    n_should_trigger: int
    n_should_not: int
    unresolved: int = 0          # spawn 异常的 probe 数(不进分母)
    token_usage: dict = {}       # 合计 token(来自各 probe 的 cc spawn)
    probes: list[ProbeResult]
