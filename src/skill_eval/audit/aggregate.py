"""aggregate: 多 transcript 对账的指令级聚合视图。

audit 的 findings 默认平铺(每 transcript × 每指令 一条),多 transcript 时是一堆散点,
看不出"哪条指令系统性被违反"。本模块把 findings 按 instruction_text 跨 transcript 聚合,
算每条指令的 pass/fail/na 分布 + fail_rate,按 fail_rate 降序——让系统性违规一眼可见。

设计决策:
- **本层不归一化**:按 instruction_text 如实分组。**同文 exact 重复**(同一句被抽成
  CONSTRAINT + ANTI_FABRICATION、同 verdict)已在上游 ``audit.dedup`` 合并,本层看不到;
  **不同 text 的扇出**(如 error-codes 抽成 REFERENCE_USAGE + HEADING_STEP 两条不同文本)
  仍是两条不同指令、在此显形为两行——这是 extractor 的毛病,聚合如实反映(倒逼治本),
  不在聚合层硬塞归一化引入新 bug。两者在**平铺 findings** 里靠 ``related`` 互标关联。
- **纯函数**:不调 LLM、不读 transcript,只对已有 findings 后处理 → 单元可测、确定。
- 判定层一行不动,只重组输出。
"""
from __future__ import annotations

from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditVerdict,
    InstructionAggregate,
)


def aggregate_by_instruction(
    findings: list[AuditFinding],
) -> list[InstructionAggregate]:
    """按 instruction_text 跨 transcript 聚合,返回每条指令的 verdict 分布(按 fail_rate 降序)。

    多 transcript 对账的核心价值:从"会话级 FAIL 计数"(无指导意义)转到"指令级违规率"
    (定位 skill 系统性问题,如"NEVER fabricate 在 75% 会话被违反")。单 transcript 时每
    指令 1 verdict,聚合退化为带 fail_rate 的列表,仍可用。

    Args:
        findings: audit 的平铺 findings(可能跨多 transcript)。

    Returns:
        每条不同 instruction_text 一个 ``InstructionAggregate``,按 fail_rate 降序
        (fail_rate 相同按 fail_count 降序,再按 instruction_text 升序,稳定可读)。
        空输入 → []。
    """
    if not findings:
        return []

    groups: dict[str, dict] = {}
    for f in findings:
        # setdefault 取首次出现的 source/category —— 同一 instruction_text 跨 transcript
        # 来自同一 SKILL.md 抽取(含 derived),source/category 一致,取首条即可。
        g = groups.setdefault(
            f.instruction_text,
            {
                "instruction_text": f.instruction_text,
                "source": f.source,
                "category": f.category,
                "pass": 0,
                "fail": 0,
                "na": 0,
                "unclear": 0,
                "fail_in_transcripts": [],
            },
        )
        if f.verdict == AuditVerdict.PASS:
            g["pass"] += 1
        elif f.verdict == AuditVerdict.FAIL:
            g["fail"] += 1
            g["fail_in_transcripts"].append(f.transcript)
        elif f.verdict == AuditVerdict.UNCLEAR:
            g["unclear"] += 1
        else:  # AuditVerdict.NA(不适用)
            g["na"] += 1

    rows: list[InstructionAggregate] = []
    for g in groups.values():
        total = g["pass"] + g["fail"] + g["na"] + g["unclear"]
        rows.append(
            InstructionAggregate(
                instruction_text=g["instruction_text"],
                source=g["source"],
                category=g["category"],
                pass_count=g["pass"],
                fail_count=g["fail"],
                na_count=g["na"],
                unclear_count=g["unclear"],
                total=total,
                fail_rate=g["fail"] / total if total else 0.0,
                fail_in_transcripts=g["fail_in_transcripts"],
            )
        )

    rows.sort(key=lambda r: (-r.fail_rate, -r.fail_count, r.instruction_text))
    return rows
