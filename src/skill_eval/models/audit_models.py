"""audit 命令的输出模型。

audit(transcript 对账)把若干 transcript 的实测行为与 skill 声明的指令逐条对照,
产出 AuditReport(若干 AuditFinding)。供后续 auditor 使用。

判定模型(2026-07-20 重构):每条 finding 有两个**正交**维度——

- **verdict(结果)**:五态。
  - ``PASS`` / ``FAIL``:已判明通过 / 违规。
  - ``NA``:不适用——规则存在但本次 transcript 场景不触发(已解决)。
  - ``UNRESOLVED``:没判成——LLM 调用 / 解析失败 / 未提及 / 畸形值,操作性失败,
    **可重跑**救回。
  - ``INDETERMINATE``:判不准——LLM 看了证据但真模糊(证据相互矛盾 / 关键缺失),
    给不出 pass/fail/na,**重跑无益**(附 indeterminate_reason)。
  没判成 vs 判不准:前者是"没拿到判定"(operational,建议重跑),后者是"拿到了判定、
  判定本身是'模糊'"(semantic,只能人工复核)。

- **method(来源/方法)**:两态—— ``PROGRAMMATIC``(程序对账:tool_trace 命中 / 子串匹配,
  不调 LLM)/ ``LLM``(LLM 给出结论,含 UNRESOLVED / INDETERMINATE:LLM 路径尝试过)。
  ``method`` 只回答"怎么查的",与"结果"正交:PASS/FAIL 既可程序判也可 LLM 判,
  没判成 / 判不准的 method 都是 LLM(那条路径产出的)。

旧版用单一 ``confidence`` 浮点(1.0/0.8/0.3)把"方法 + 是否判不准"揉成一个假浮点,
既不可叠加又难解释,已废弃。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class AuditVerdict(str, Enum):
    """单条指令的对账结论(五态)。

    PASS / FAIL / NA 是已解决结论;UNRESOLVED / INDETERMINATE 是未解决(本次结果
    不完整),audit() 据二者汇总不同警告。
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NA = "N/A"  # 不适用:规则存在但本次 transcript 场景不触发(applicable=false / STEP-"na"),已解决
    UNRESOLVED = "UNRESOLVED"  # 没判成:LLM 调用/解析失败/未提及/畸形值,操作性失败,可重跑
    INDETERMINATE = "INDETERMINATE"  # 判不准:LLM 看了证据但真模糊,给不出结论,重跑无益(附 reason)


class AuditMethod(str, Enum):
    """判定方法(怎么查的)——与 verdict(结果)正交。

    PROGRAMMATIC:程序对账(tool_trace 命中 / 输出子串匹配),确定性、不调 LLM。
    LLM:LLM 给出结论(含没判成 / 判不准:LLM 路径尝试过但未出明确 pass/fail/na)。
    """

    PROGRAMMATIC = "PROGRAMMATIC"
    LLM = "LLM"


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

    ``source`` 用 str 解耦,不直接依赖 batch_models 的 InstructionSource 枚举——
    这样 audit 模型即便 InstructionSource 扩展也无需改动。注意 ``source`` 是**指令
    的规则类型**(CONSTRAINT / STEP …),与 ``method``(判定方法)是两个不同维度。

    向后兼容:旧 JSON 用 ``verdict="UNCLEAR"`` + ``confidence`` 浮点(假浮点已废)。
    ``_legacy_normalize`` 在 validate 前把旧 ``UNCLEAR`` 折成 ``UNRESOLVED``,
    并据旧 ``confidence`` 推断 ``method``(>=1.0 → PROGRAMMATIC,否则 LLM),
    使旧 run 产物仍可加载。
    """

    instruction_text: str
    # 该指令在声明原文里所属 bullet 的完整文本(含主语上下文)。显示层用它补全被切的主语;
    # **判定不读它**(judge 吃 instruction_text)。透传自 InstructionCheck.source_excerpt,
    # 缺省空(合成文案 / derived / 旧 JSON)。加在 finding 上供下游(insight)渲染。
    source_excerpt: str = ""
    source: str  # InstructionSource 的值(如 "CONSTRAINT")——指令的规则类型
    verdict: AuditVerdict
    method: AuditMethod  # 判定方法(PROGRAMMATIC / LLM)——怎么查的
    category: AuditCategory
    evidence: str  # 中文,为何这个 verdict(INDETERMINATE 时此即 indeterminate_reason)
    transcript: str  # 该 finding 对应的 transcript 文件名
    derived_from_transcript: bool = False  # 反推声明来的指令标 True
    # findings 后处理(audit.dedup.post_process_findings)填:
    seq: int = 0  # #1..#N:去重后编排的序号(供 related 引用 + 报告里 #N 可读定位)
    sources: list[str] = Field(default_factory=list)  # 同文去重后合并的所有来源;单条=[source]
    related: list[str] = Field(default_factory=list)  # 同行为跨类(Case B)关联的其它 finding 序号,如 ["#9"]
    # FAIL 噪声判别(audit.noise.annotate_noise)填,只对 FAIL 标;None/[] 向后兼容旧 JSON
    noise_label: str | None = None  # "tool_noise" / "skill_issue" / "uncertain";非 FAIL 为 None
    noise_reasons: list[str] = Field(default_factory=list)  # 命中理由(中文);非 FAIL 为空
    # finding 指向的 turn 序号(对应 transcript 里 [Tn] 标记)。
    # 仅 prohibition / step 类(走 transcript_text + turn 引用的 LLM judge)在 FAIL / INDETERMINATE
    # 时由 LLM 填;conditional / output 类的 FAIL 是段级缺席(trace 全程未调用 / 未读取 X、期望格式
    # 全程未出现),本就无具体 turn、恒空。PASS / N/A 或其它段级缺席亦为空。
    # 序号语义:cc / structured 切片保留会话全局序号;opencode 切片经 message 子集往返重建、按段内
    # 位置 re-stamp,seq 是段内 local(非会话全局)。旧 JSON 无此字段 → 空(向后兼容)。
    turn_refs: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_normalize(cls, data: object) -> object:
        """旧 JSON 兼容:UNCLEAR→UNRESOLVED,confidence→method。

        旧版 verdict ``UNCLEAR`` 现拆成 ``UNRESOLVED``(没判成)/ ``INDETERMINATE``(判不准)。
        旧 ``UNCLEAR`` 实际只装"没判成"(LLM 失败/解析失败/未提及),故折成 ``UNRESOLVED``。
        旧 ``confidence`` 浮点(假浮点)废:>=1.0 是程序判 → PROGRAMMATIC,其余 LLM。
        新数据有 ``method`` 则不动。纯加载侧兼容,不影响新产出。**拷贝后改**,不动调用方 dict。
        """
        if isinstance(data, dict):
            data = dict(data)
            if data.get("verdict") == "UNCLEAR":
                data["verdict"] = "UNRESOLVED"
            if "method" not in data and "confidence" in data:
                try:
                    data["method"] = (
                        AuditMethod.PROGRAMMATIC if float(data["confidence"]) >= 1.0 else AuditMethod.LLM
                    )
                except (TypeError, ValueError):
                    data["method"] = AuditMethod.LLM
        return data


class InstructionAggregate(BaseModel):
    """单条指令跨 transcript 的对账聚合(见 audit.aggregate.aggregate_by_instruction)。"""

    instruction_text: str
    # 该指令所属 bullet 的完整原文(含主语);透传自 AuditFinding,显示用、判定不读。缺省空。
    source_excerpt: str = ""
    source: str
    category: AuditCategory
    pass_count: int
    fail_count: int
    na_count: int  # 不适用(applicable=false / STEP-"na")
    unresolved_count: int = 0  # 没判成(LLM 调用/解析失败/未提及),操作性失败、可重跑
    indeterminate_count: int = 0  # 判不准(LLM 看了证据但真模糊),semantic、重跑无益
    total: int
    fail_rate: float  # fail_count / total(total=0 时 0.0)
    fail_in_transcripts: list[str]  # 哪些 transcript 里判 FAIL(定位用)
    na_in_transcripts: list[str] = Field(default_factory=list)  # 哪些 transcript 判 N/A(场景不适用,定位用);默认空兼容旧 report
    fail_seqs: list[int] = Field(default_factory=list)  # FAIL findings 的明细 #N(汇总表交叉引用到明细用)
    na_seqs: list[int] = Field(default_factory=list)  # N/A findings 的明细 #N
    # FAIL 噪声判别(audit.noise.annotate_aggregate_noise)填,只对 fail_count>0 标;None/[] 向后兼容
    noise_label: str | None = None  # "tool_noise" / "uncertain";无 FAIL 为 None
    noise_reasons: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    """一个 skill 一次 audit 的完整报告。"""

    skill_name: str
    transcripts: list[str]
    transcript_sources: list[str] = Field(default_factory=list)  # 与 transcripts 同序并行;"cc"/"opencode"/...(未来扩)。标每个 transcript 来自哪侧;旧 report 无此字段 → 空(向后兼容)
    summary: dict  # {"total": N, "pass": X, "fail": Y, "na": Z(不适用), "unresolved": U(没判成), "indeterminate": I(判不准)}
    findings: list[AuditFinding]
    # 多 transcript 对账的指令级聚合(按 fail_rate 降序);单 transcript 也有(每指令 1 verdict)。
    by_instruction: list[InstructionAggregate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # refine(LLM 精筛伪指令)砍掉的条数(len(before) - len(after))。
    # 0 = 没开 refine_instructions 或开了但没砍任何条;向后兼容旧 report(默认 0)。
    refine_dropped_count: int = 0
    # 本次 audit 所有 LLM 判定调用(refine + 条件/禁令/步骤判官 × 批)的 token 用量合计。
    # call_claude json 模式抓 usage,经 TokenUsageAccumulator 累加:{input_tokens,
    # output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    # total_cost_usd, calls}。空 dict = 无 LLM 调用或旧 report(默认空,向后兼容)。
    token_usage: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0  # Phase 2.5: audit 墙钟(audit() 头尾 perf_counter;判官 token 在 token_usage)
