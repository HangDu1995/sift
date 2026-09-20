"""AuditFinding.turn_refs 字段(Task 3):承载 finding 指向的全局 turn 序号。

verdict 为 FAIL / INDETERMINATE 时,这里存"该违规涉及哪些全局 turn"——
Task 4 会把 LLM 引用的 turn 填进来;PASS / N/A 或 segment 级别缺席时为空。
本文件只验字段存在 + 默认空 + 往返序列化(填值由 Task 4 接)。
"""

from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
)


def test_finding_has_turn_refs_default_empty():
    f = AuditFinding(
        instruction_text="x",
        source="HEADING_STEP",
        verdict=AuditVerdict.FAIL,
        category=AuditCategory.STEP,
        evidence="e",
        method=AuditMethod.LLM,
        transcript="s.jsonl",
    )
    assert f.turn_refs == []


def test_finding_turn_refs_roundtrip():
    f = AuditFinding(
        instruction_text="x",
        source="HEADING_STEP",
        verdict=AuditVerdict.FAIL,
        category=AuditCategory.STEP,
        evidence="e",
        method=AuditMethod.LLM,
        transcript="s.jsonl",
        turn_refs=[142, 156],
    )
    d = f.model_dump()
    assert d["turn_refs"] == [142, 156]
    assert AuditFinding(**d).turn_refs == [142, 156]  # 往返
