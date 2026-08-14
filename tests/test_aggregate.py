"""aggregate_by_instruction 单元测试:多 transcript 指令级聚合(违规率)。

纯函数测试,不调 LLM——直接造 AuditFinding,验证聚合逻辑:
- 跨 transcript 按 instruction_text 分组,算 pass/fail/na 分布 + fail_rate;
- 按 fail_rate 降序(系统性违规在前);
- 单 transcript 仍可用(每指令 1 verdict);
- 不去重:如实按 text 分组(extractor 重复抽取会显形多行)。
"""
from __future__ import annotations

from sift.audit.aggregate import aggregate_by_instruction
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
)


def _f(
    text: str,
    verdict: AuditVerdict,
    transcript: str,
    source: str = "TOOL_REQUIREMENT",
    category: AuditCategory = AuditCategory.CONDITIONAL,
    seq: int = 0,
) -> AuditFinding:
    return AuditFinding(
        instruction_text=text,
        source=source,
        verdict=verdict,
        method=AuditMethod.PROGRAMMATIC,
        category=category,
        evidence="e",
        transcript=transcript,
        seq=seq,
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

    def test_source_excerpt_taken_from_first_finding(self):
        """聚合行的 source_excerpt 取首次出现 finding 的(主语上下文,显示用、判定不读)。"""
        f1 = _f("必须存在", AuditVerdict.PASS, "s1", "CONSTRAINT", AuditCategory.PROHIBITION)
        f1.source_excerpt = "design_doc_cases.json 必须存在"
        f2 = _f("必须存在", AuditVerdict.FAIL, "s2", "CONSTRAINT", AuditCategory.PROHIBITION)
        rows = aggregate_by_instruction([f1, f2])
        assert len(rows) == 1
        assert rows[0].source_excerpt == "design_doc_cases.json 必须存在"

    def test_headline_taken_from_first_finding(self):
        """聚合行的 headline 取首次出现 finding 的(人话标题,显示用、判定不读)。"""
        f1 = _f("必须存在", AuditVerdict.PASS, "s1", "CONSTRAINT", AuditCategory.PROHIBITION)
        f1.headline = "design_doc_cases.json 必须存在(人话)"
        f2 = _f("必须存在", AuditVerdict.FAIL, "s2", "CONSTRAINT", AuditCategory.PROHIBITION)
        rows = aggregate_by_instruction([f1, f2])
        assert len(rows) == 1
        assert rows[0].headline == "design_doc_cases.json 必须存在(人话)"

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

    def test_na_in_transcripts_and_seqs_track_where_instruction_was_na(self):
        """na_in_transcripts 记录哪几段判 N/A,na_seqs 记录对应明细 #N(供 N/A 聚合表
        交叉引用到明细)。同指令两段都 N/A → 2 条 transcript + 2 个 #N。

        例:security-policy 两段日志都没安全事件 → 两段都 N/A(na/total 2/2);
        error-codes 只 span2 没触发 → 1 条。"""
        findings = [
            _f("Use ref: security-policy.md", AuditVerdict.NA, "span1", seq=3),
            _f("Use ref: security-policy.md", AuditVerdict.NA, "span2", seq=7),
            _f("Use ref: error-codes.md", AuditVerdict.PASS, "span1", seq=1),
            _f("Use ref: error-codes.md", AuditVerdict.NA, "span2", seq=5),
        ]
        rows = {r.instruction_text: r for r in aggregate_by_instruction(findings)}
        sec = rows["Use ref: security-policy.md"]
        assert sec.na_count == 2 and sec.total == 2
        assert set(sec.na_in_transcripts) == {"span1", "span2"}
        assert set(sec.na_seqs) == {3, 7}
        ec = rows["Use ref: error-codes.md"]
        assert ec.na_count == 1
        assert set(ec.na_in_transcripts) == {"span2"}
        assert set(ec.na_seqs) == {5}

    def test_unresolved_and_indeterminate_buckets_split(self):
        """没判成(UNRESOLVED)与判不准(INDETERMINATE)分桶计:同指令跨 4 段各一态,
        聚合后 unresolved_count=1 / indeterminate_count=1,且都计入 total(稀释 fail_rate)。

        回归重构:旧 unclear 单桶现拆二;total = pass+fail+na+unresolved+indeterminate。"""
        findings = [
            _f("X", AuditVerdict.PASS, "s1"),
            _f("X", AuditVerdict.FAIL, "s2"),
            _f("X", AuditVerdict.UNRESOLVED, "s3"),
            _f("X", AuditVerdict.INDETERMINATE, "s4"),
        ]
        rows = aggregate_by_instruction(findings)
        assert len(rows) == 1
        r = rows[0]
        assert r.pass_count == 1 and r.fail_count == 1
        assert r.unresolved_count == 1 and r.indeterminate_count == 1
        assert r.na_count == 0
        assert r.total == 4  # 全 4 态计入 total
        assert r.fail_rate == 0.25  # 1/4(unresolved/indeterminate 稀释 fail_rate,同旧 unclear 行为)
