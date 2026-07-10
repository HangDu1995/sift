"""aggregate_by_instruction 单元测试:多 transcript 指令级聚合(违规率)。

纯函数测试,不调 LLM——直接造 AuditFinding,验证聚合逻辑:
- 跨 transcript 按 instruction_text 分组,算 pass/fail/na 分布 + fail_rate;
- 按 fail_rate 降序(系统性违规在前);
- 单 transcript 仍可用(每指令 1 verdict);
- 不去重:如实按 text 分组(extractor 重复抽取会显形多行)。
"""
from __future__ import annotations

from skill_eval.audit.aggregate import aggregate_by_instruction
from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditVerdict,
)


def _f(
    text: str,
    verdict: AuditVerdict,
    transcript: str,
    source: str = "TOOL_REQUIREMENT",
    category: AuditCategory = AuditCategory.CONDITIONAL,
) -> AuditFinding:
    return AuditFinding(
        instruction_text=text,
        source=source,
        verdict=verdict,
        category=category,
        evidence="e",
        confidence=1.0,
        transcript=transcript,
    )


class TestAggregateByInstruction:
    def test_groups_across_transcripts_and_ranks_by_fail_rate(self):
        """NEVER fabricate 跨 4 会话 3 FAIL(75%)应排在全 PASS 的指令前。"""
        findings = [
            _f("NEVER fabricate", AuditVerdict.PASS, "s1", "CONSTRAINT", AuditCategory.PROHIBITION),
            _f("NEVER fabricate", AuditVerdict.FAIL, "s2", "CONSTRAINT", AuditCategory.PROHIBITION),
            _f("NEVER fabricate", AuditVerdict.FAIL, "s3", "CONSTRAINT", AuditCategory.PROHIBITION),
            _f("NEVER fabricate", AuditVerdict.FAIL, "s4", "CONSTRAINT", AuditCategory.PROHIBITION),
            _f("Read input log", AuditVerdict.PASS, "s1"),
            _f("Read input log", AuditVerdict.PASS, "s2"),
            _f("Read input log", AuditVerdict.PASS, "s3"),
            _f("Read input log", AuditVerdict.PASS, "s4"),
        ]
        rows = aggregate_by_instruction(findings)
        assert len(rows) == 2
        # 75% fail 的系统性违规排第一
        assert rows[0].instruction_text == "NEVER fabricate"
        assert rows[0].fail_count == 3
        assert rows[0].total == 4
        assert rows[0].fail_rate == 0.75
        assert set(rows[0].fail_in_transcripts) == {"s2", "s3", "s4"}
        # 全 PASS 的排后面,fail_rate 0
        assert rows[1].instruction_text == "Read input log"
        assert rows[1].fail_rate == 0.0
        assert rows[1].pass_count == 4

    def test_single_transcript_still_works(self):
        """单 transcript:每指令 1 verdict,fail 的排前。聚合在单会话退化为列表。"""
        findings = [
            _f("A", AuditVerdict.FAIL, "s1"),
            _f("B", AuditVerdict.PASS, "s1"),
            _f("C", AuditVerdict.NA, "s1"),
        ]
        rows = aggregate_by_instruction(findings)
        assert len(rows) == 3
        assert rows[0].instruction_text == "A"  # fail(fail_rate 1.0)排前
        assert rows[0].total == 1
        assert rows[0].fail_rate == 1.0

    def test_empty_findings(self):
        assert aggregate_by_instruction([]) == []

    def test_no_dedup_same_text_merges_different_text_keeps_separate(self):
        """不去重:相同 instruction_text 跨 transcript 合并一组;不同 text(即便同实体)分开。

        extractor 把 error-codes 抽成两条不同 text 时,聚合如实显示两行——
        重复抽取显形,倒逼 extractor 治本,而非聚合层硬塞归一化。"""
        findings = [
            _f("Use reference file: references/error-codes.md", AuditVerdict.FAIL, "s1", "REFERENCE_USAGE"),
            _f("Classify each error — Read references/error-codes.md", AuditVerdict.FAIL, "s1", "HEADING_STEP", AuditCategory.STEP),
        ]
        rows = aggregate_by_instruction(findings)
        assert len(rows) == 2  # 两条不同 text,不合并
        assert all(r.fail_count == 1 for r in rows)

    def test_fail_rate_tiebreak_by_fail_count_then_text(self):
        """同 fail_rate 时,按 fail_count 降序、再按 text 升序(稳定可读)。"""
        findings = [
            _f("zebra", AuditVerdict.FAIL, "s1"),
            _f("zebra", AuditVerdict.PASS, "s2"),  # fail_rate 0.5, fail=1
            _f("alpha", AuditVerdict.FAIL, "s1"),
            _f("alpha", AuditVerdict.FAIL, "s2"),
            _f("alpha", AuditVerdict.PASS, "s3"),  # fail_rate 0.67, fail=2
        ]
        rows = aggregate_by_instruction(findings)
        assert rows[0].instruction_text == "alpha"  # fail_rate 0.67 > 0.5
