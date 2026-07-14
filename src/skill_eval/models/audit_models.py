"""audit 命令的输出模型。

audit(transcript 对账)把若干 transcript 的实测行为与 skill 声明的指令逐条对照,
产出 AuditReport(若干 AuditFinding)。供后续 auditor 使用。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AuditVerdict(str, Enum):
    """单条指令的对账结论。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NA = "N/A"


class AuditCategory(str, Enum):
    """指令类别(对应 InstructionSource 的语义分组)。

    conditional: 条件性(有触发条件:TOOL_REQUIREMENT / REFERENCE_USAGE)
    prohibition: 禁令类(CONSTRAINT 禁止性 / ANTI_FABRICATION)
    step:        HEADING_STEP
    output:      OUTPUT_FORMAT
    """

    CONDITIONAL = "conditional"
    PROHIBITION = "prohibition"
    STEP = "step"
    OUTPUT = "output"


class AuditFinding(BaseModel):
    """单条指令的对账结果。

    source 用 str 解耦,不直接依赖 batch_models 的 InstructionSource 枚举——
    这样 audit 模型即便 InstructionSource 扩展也无需改动。
    """

    instruction_text: str
    source: str  # InstructionSource 的值(如 "CONSTRAINT")
    verdict: AuditVerdict
    category: AuditCategory
    evidence: str  # 中文,为何这个 verdict
    confidence: float
    transcript: str  # 该 finding 对应的 transcript 文件名
    derived_from_transcript: bool = False  # 反推声明来的指令标 True
    # findings 后处理(audit.dedup.post_process_findings)填:
    seq: int = 0  # #1..#N:去重后编排的序号(供 related 引用 + 报告里 #N 可读定位)
    sources: list[str] = Field(default_factory=list)  # 同文去重后合并的所有来源;单条=[source]
    related: list[str] = Field(default_factory=list)  # 同行为跨类(Case B)关联的其它 finding 序号,如 ["#9"]


class InstructionAggregate(BaseModel):
    """单条指令跨 transcript 的对账聚合(见 audit.aggregate.aggregate_by_instruction)。"""

    instruction_text: str
    source: str
    category: AuditCategory
    pass_count: int
    fail_count: int
    na_count: int
    total: int
    fail_rate: float  # fail_count / total(total=0 时 0.0)
    fail_in_transcripts: list[str]  # 哪些 transcript 里判 FAIL(定位用)


class AuditReport(BaseModel):
    """一个 skill 一次 audit 的完整报告。"""

    skill_name: str
    transcripts: list[str]
    summary: dict  # {"total": N, "pass": X, "fail": Y, "na": Z}
    findings: list[AuditFinding]
    # 多 transcript 对账的指令级聚合(按 fail_rate 降序);单 transcript 也有(每指令 1 verdict)。
    by_instruction: list[InstructionAggregate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
