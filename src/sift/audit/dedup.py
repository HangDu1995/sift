"""dedup: findings 去重 + 编号 + 关联(转录对账的后处理)。

audit 的 findings 平铺里有两类"看着多个、其实是一个",都在本模块治:

- **Case A 同文重复**:同一句被 extractor 抽成 `CONSTRAINT` + `ANTI_FABRICATION`(都属
  prohibition 类),同 transcript、同 verdict → **合并成一条**,``sources`` 记两个来源。
  去重键 ``(instruction_text, category, transcript, verdict)``——四者全同、仅 ``source``
  不同才合并;verdict 不同则都留(不掩盖"同句被判出矛盾 verdict"的信号,零信息损失)。
  跨 transcript 不合并(不同会话,各自一条)。

- **Case B 同行为跨类**:同一行为(如"没读 references/error-codes.md")同时违反 条件性
  + 步骤 两条**不同**的规则 → **不合并**(确实是 SKILL 里两条不同规则),但互标
  ``related=["#N"]``。关联键:同 transcript + ``instruction_text`` 共享一个
  ``references/<x>`` 或 ``scripts/<x>`` token(保守、确定、无 LLM)。**任何 verdict 都标**
  (FAIL/PASS 都关联——同一行为无论过没过,都该让用户一眼看到它跨了几条规则)。

去重后给每条编 ``#1..#N``(``seq``),``related`` 就引用序号,报告里 #N 可读、可定位。

**纯函数、不调 LLM**——对已有 findings 后处理,确定、可单测。auditor 在 transcript 循环后
调 :func:`post_process_findings`,使 unverified/summary/by_instruction 都基于去重后 findings。
"""
from __future__ import annotations

import re

from sift.models.audit_models import AuditFinding

# references/<x> 或 scripts/<x>(extractor 抽 REFERENCE_USAGE 的同一套路径词)。
# 只认结构化路径 token,不认裸文件名(app.log 之类)——避免通用词误关联。可扩展。
_REF_RE = re.compile(r"(?:references|scripts)/[\w\-.]+")


def _ref_tokens(text: str) -> set[str]:
    """instruction_text 里的 references/scripts 路径 token(去尾随句点、去重)。

    尾随 ASCII 句点剥离(``foo.md.``→``foo.md``):``[\\w\\-.]+`` 贪婪会吃掉句末的点,
    而文件名不以点结尾,剥之安全且避免"路径后跟句号"漏关联。
    """
    return {tok.rstrip(".") for tok in _REF_RE.findall(text or "")}


def dedup_exact_duplicates(findings: list[AuditFinding]) -> list[AuditFinding]:
    """合并 (instruction_text, category, transcript, verdict) 全同的 finding。

    仅 ``source`` 不同的归并成一条,``sources`` = 该组所有 source(去重排序);其余字段
    取首条(verdict 不同本就不会进同组)。单条也填 ``sources=[source]``。保持首次出现顺序。
    """
    order: list[tuple] = []
    rep: dict[tuple, AuditFinding] = {}
    sources_by_key: dict[tuple, set[str]] = {}
    for f in findings:
        key = (f.instruction_text, f.category, f.transcript, f.verdict)
        if key not in rep:
            rep[key] = f
            order.append(key)
            sources_by_key[key] = set()
        sources_by_key[key].add(f.source)
    out: list[AuditFinding] = []
    for key in order:
        f = rep[key]
        f.sources = sorted(sources_by_key[key])
        out.append(f)
    return out


def assign_sequence(findings: list[AuditFinding]) -> list[AuditFinding]:
    """给每条编 1-based 序号(去重后顺序)。原地改 ``seq``,返回同一列表。"""
    for i, f in enumerate(findings, start=1):
        f.seq = i
    return findings


def link_related(findings: list[AuditFinding]) -> list[AuditFinding]:
    """Case B:同 transcript + instruction_text 共享 ref/scripts token → 互标 related。

    任何 verdict 都标。``related`` = 关联到的其它 finding 序号(如 ``["#9"]``),排除自己。
    原地改 ``related``,返回同一列表。需先 :func:`assign_sequence`(related 引 seq)。
    """
    # (transcript, token) → 该组所有 seq
    by_token: dict[tuple[str, str], list[int]] = {}
    for f in findings:
        for tok in _ref_tokens(f.instruction_text):
            by_token.setdefault((f.transcript, tok), []).append(f.seq)
    for f in findings:
        related: set[int] = set()
        for tok in _ref_tokens(f.instruction_text):
            for other in by_token.get((f.transcript, tok), ()):
                if other != f.seq:
                    related.add(other)
        f.related = [f"#{s}" for s in sorted(related)]
    return findings


def post_process_findings(findings: list[AuditFinding]) -> list[AuditFinding]:
    """去重 → 编号 → 关联(依次,related 需 seq)。auditor 在汇总前调一次。"""
    out = dedup_exact_duplicates(findings)
    assign_sequence(out)
    link_related(out)
    return out
