"""noise: FAIL 噪声 / skill 判别(finding 级 + 聚合级后处理)。

audit 产出的 FAIL finding 里混着两类——

- **工具噪声**(tool_noise):extractor 误把资源描述当 HEADING_STEP、evidence 自相矛盾
  ("本场景不涉及"却判 FAIL)、LLM 判定且带跨类关联(稳定性存疑)。这些不是 skill 问题,
  改 skill 治不了,应据信号忽略或修工具。
- **skill 问题**(skill_issue):硬约束(CONSTRAINT / ANTI_FABRICATION)被程序判定 FAIL
  ——确定、可复现,大概率真是 skill 没做到。
- 其余 FAIL → uncertain(单条信号不足,看聚合分布或人工复核)。

每个 FAIL finding 标 ``noise_label`` + ``noise_reasons``;聚合级(``InstructionAggregate``)
再看 fail/na 分布给一个汇总判别。**优先级 tool_noise > skill_issue > uncertain**(取最严:
只要有一条噪声信号,就归 tool_noise,避免据噪声误改 skill)。

**纯函数、不调 LLM、不读 transcript**——对已有 findings/aggregates 后处理,确定、可单测。
对齐 dedup.py 定位。``noise_label`` 用 str(不引入 enum),值 ``"tool_noise"`` /
``"skill_issue"`` / ``"uncertain"``。
"""

from __future__ import annotations

import re

from sift.models.audit_models import (
    AuditFinding,
    AuditMethod,
    AuditVerdict,
    InstructionAggregate,
)

# instruction_text 里的资源链接:[..](xxx.md/py/sh/json/txt)。extractor 易把这种资源描述
# 误提为 HEADING_STEP(它确实是标题下一行),但它是"参考资源"而非"执行步骤"。
_RESOURCE_LINK_RE = re.compile(r"\]\([^)]+\.(?:md|py|sh|json|txt)\)")
# 更宽的 markdown 链接信号:任何 ]( 都算(资源描述常见形态)。
_MARKDOWN_LINK_RE = re.compile(r"\]\(")

# evidence 自相矛盾信号:verdict=FAIL 但 evidence 称"不涉及/不走/属流程"等。
# 命中即说明判定本身可能拧着(把 NA 场景判成了 FAIL),典型工具噪声。
_CONTRADICTION_RE = re.compile(r"不涉及|不走|不涉及该|本场景不|属流程")

# 硬约束来源:程序判定 FAIL 大概率真 skill 问题。
_HARD_CONSTRAINT_SOURCES = frozenset({"CONSTRAINT", "ANTI_FABRICATION"})


def _contradicts(evidence: str) -> bool:
    return bool(_CONTRADICTION_RE.search(evidence or ""))


def _looks_like_resource_link(text: str) -> bool:
    return bool(_RESOURCE_LINK_RE.search(text or "") or _MARKDOWN_LINK_RE.search(text or ""))


def annotate_noise(findings: list[AuditFinding]) -> list[AuditFinding]:
    """给每条 FAIL finding 标 noise_label + noise_reasons;非 FAIL 置 None/[]。

    启发式(初版),原地改、返回同一列表(对齐 dedup.assign_sequence / link_related):

    - **tool_noise**(命中 ≥1 信号):
      1. ``source == "HEADING_STEP"`` 且 instruction_text 像资源链接(``[x](y.md)``)→
         「资源描述被当步骤」
      2. evidence 自相矛盾(FAIL 却称"不涉及/不走/属流程")→「evidence 称不涉及却判 FAIL」
      3. ``method == LLM`` 且 ``related`` 非空 →「LLM 判定,有跨类关联,稳定性存疑」
    - **skill_issue**:``source ∈ {CONSTRAINT, ANTI_FABRICATION}`` 且 ``method == PROGRAMMATIC``
      且 evidence 无"不涉及"矛盾 →「硬约束程序判定 FAIL」
    - **uncertain**:无上述信号的 FAIL →「单条信号不足,看汇总分布或人工复核」

    多信号优先级:tool_noise > skill_issue > uncertain。tool_noise 内多信号 reasons 全收
    (告诉用户具体命了哪几条);skill_issue / uncertain 各只附一条 reason。
    """
    for f in findings:
        if f.synthetic:
            # 合成 finding(ORDER)豁免:启发式 3 的「LLM+related」信号在 ORDER 上
            # 语义是「pair 两端」不是「跨类稳定性存疑」,恒命中会把全部 ORDER FAIL 打成
            # 工具噪声;聚合级规则(noise.py 按 HEADING_STEP 行判)因 synthetic 不进
            # by_instruction 天然不触发。置 None/[](同非 FAIL)。
            f.noise_label = None
            f.noise_reasons = []
            continue
        if f.verdict != AuditVerdict.FAIL:
            f.noise_label = None
            f.noise_reasons = []
            continue

        reasons: list[str] = []
        # tool_noise 信号(任一命中即 tool_noise,reasons 全收)
        if f.source == "HEADING_STEP" and _looks_like_resource_link(f.instruction_text):
            reasons.append("资源描述被当步骤")
        if _contradicts(f.evidence):
            reasons.append("evidence 称不涉及却判 FAIL")
        if f.method == AuditMethod.LLM and f.related:
            reasons.append("LLM 判定,有跨类关联,稳定性存疑")

        if reasons:
            f.noise_label = "tool_noise"
            f.noise_reasons = reasons
        elif (
            f.source in _HARD_CONSTRAINT_SOURCES
            and f.method == AuditMethod.PROGRAMMATIC
            and not _contradicts(f.evidence)
        ):
            f.noise_label = "skill_issue"
            f.noise_reasons = ["硬约束程序判定 FAIL"]
        else:
            f.noise_label = "uncertain"
            f.noise_reasons = ["单条信号不足,看汇总分布或人工复核"]
    return findings


def annotate_aggregate_noise(aggregates: list[InstructionAggregate]) -> list[InstructionAggregate]:
    """给每条 fail_count>0 的聚合标 noise_label + noise_reasons;无 FAIL 置 None/[]。

    聚合级只看 fail/na 分布给一个粗判别(细判别看 finding 级):

    - **tool_noise**:``fail_count > 0`` 且 ``na_count > fail_count`` 且
      ``source == "HEADING_STEP"`` →「NA{n}多 FAIL{f},条件性资源被当无条件步骤」
      (大量 NA 说明这条步骤是条件性的,少数 transcript 触发却被当无条件 FAIL,典型噪声)
    - **uncertain**:有 FAIL 但不满足上述 →「分布无显著噪声特征,看 finding 级标注或人工复核」
    """
    for a in aggregates:
        if a.fail_count <= 0:
            a.noise_label = None
            a.noise_reasons = []
            continue
        if a.source == "HEADING_STEP" and a.na_count > a.fail_count:
            a.noise_label = "tool_noise"
            a.noise_reasons = [f"NA{a.na_count}多FAIL{a.fail_count},条件性资源被当无条件步骤"]
        else:
            a.noise_label = "uncertain"
            a.noise_reasons = ["分布无显著噪声特征,看 finding 级标注或人工复核"]
    return aggregates
