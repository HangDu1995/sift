"""tests/test_audit_dedup.py — findings 去重(Case A 同文)+ 编号 + 关联(Case B 跨类同行为)。

audit 的 findings 平铺里有两类"看着多个、其实是一个"的情况:
- Case A 同文重复:同一句被 extractor 抽成 CONSTRAINT + ANTI_FABRICATION,同类别
  (prohibition)、同 verdict → 合并成一条,sources 记两个来源。
- Case B 同行为跨类:同一行为(如没读 error-codes.md)同时违反 条件性 + 步骤 两条
  *不同* 规则 → 不合并(确实是两条规则),但互标 related=["#N"],报告里 #N 可定位。

去重后给每条编 #1..#N(related 引用序号、报告可读)。post_process_findings 串三步,
audit() 在 transcript 循环后调它(unverified/summary/by_instruction 都基于去重后 findings)。
"""
from __future__ import annotations

import json

from sift.audit.auditor import TranscriptAuditor
from sift.audit.dedup import (
    assign_sequence,
    dedup_exact_duplicates,
    link_related,
    post_process_findings,
)
from sift.models.audit_models import AuditCategory, AuditFinding, AuditMethod, AuditVerdict


def _f(
    text: str,
    source: str = "REFERENCE_USAGE",
    verdict: AuditVerdict = AuditVerdict.PASS,
    category: AuditCategory = AuditCategory.CONDITIONAL,
    transcript: str = "t.jsonl",
    method: AuditMethod = AuditMethod.PROGRAMMATIC,
    evidence: str = "e",
    derived: bool = False,
) -> AuditFinding:
    return AuditFinding(
        instruction_text=text, source=source, verdict=verdict, category=category,
        evidence=evidence, method=method, transcript=transcript,
        derived_from_transcript=derived,
    )


class TestCaseADedup:
    def test_merges_same_text_category_transcript_verdict(self) -> None:
        a = _f("禁止编造", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        b = _f("禁止编造", "ANTI_FABRICATION", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 1
        assert out[0].sources == ["ANTI_FABRICATION", "CONSTRAINT"]  # 排序去重

    def test_keeps_when_verdict_differs(self) -> None:
        a = _f("禁止编造", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        b = _f("禁止编造", "ANTI_FABRICATION", AuditVerdict.PASS, AuditCategory.PROHIBITION)
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 2  # verdict 不同 → 都留(不掩盖矛盾)

    def test_keeps_across_transcripts(self) -> None:
        a = _f("禁止编造", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION, transcript="t1")
        b = _f("禁止编造", "ANTI_FABRICATION", AuditVerdict.FAIL, AuditCategory.PROHIBITION, transcript="t2")
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 2  # 跨 transcript 不合并(不同会话)

    def test_singleton_populates_sources(self) -> None:
        a = _f("读 X", "REFERENCE_USAGE")
        out = dedup_exact_duplicates([a])
        assert out[0].sources == ["REFERENCE_USAGE"]

    def test_preserves_order(self) -> None:
        items = [_f(f"t{i}", "REFERENCE_USAGE") for i in range(3)]
        out = dedup_exact_duplicates(items)
        assert [f.instruction_text for f in out] == ["t0", "t1", "t2"]

    def test_three_way_merge(self) -> None:
        # 极端:三个来源同文同 verdict → 合一,sources 三者
        a = _f("x", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        b = _f("x", "ANTI_FABRICATION", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        c = _f("x", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)  # 同 source 再来一条
        out = dedup_exact_duplicates([a, b, c])
        assert len(out) == 1
        assert out[0].sources == ["ANTI_FABRICATION", "CONSTRAINT"]  # source 去重


class TestSequence:
    def test_assigns_1_based_in_order(self) -> None:
        items = [_f("a"), _f("b"), _f("c")]
        assign_sequence(items)
        assert [f.seq for f in items] == [1, 2, 3]


class TestCaseBLink:
    def test_links_by_shared_ref_token(self) -> None:
        a = _f("Use reference file: references/error-codes.md", "REFERENCE_USAGE",
               AuditVerdict.FAIL, AuditCategory.CONDITIONAL)
        b = _f("分类每个错误 — 读取 references/error-codes.md,映射到已知码。", "HEADING_STEP",
               AuditVerdict.FAIL, AuditCategory.STEP)
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)  # a=#1, b=#2
        link_related(items)
        assert items[0].related == ["#2"]
        assert items[1].related == ["#1"]

    def test_no_link_without_shared_token(self) -> None:
        a = _f("禁止编造错误码", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        b = _f("写报告 error-report.json", "HEADING_STEP", AuditVerdict.FAIL, AuditCategory.STEP)
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == [] and items[1].related == []

    def test_no_link_across_transcripts(self) -> None:
        a = _f("读 references/x.md", "REFERENCE_USAGE", transcript="t1")
        b = _f("步骤 读取 references/x.md", "HEADING_STEP", transcript="t2")
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == [] and items[1].related == []

    def test_links_any_verdict(self) -> None:
        # 都标:FAIL + PASS 都关联(不只 FAIL)
        a = _f("读 references/x.md", "REFERENCE_USAGE", AuditVerdict.PASS, AuditCategory.CONDITIONAL)
        b = _f("步骤 读 references/x.md", "HEADING_STEP", AuditVerdict.PASS, AuditCategory.STEP)
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == ["#2"] and items[1].related == ["#1"]

    def test_no_self_link(self) -> None:
        a = _f("读 references/x.md", "REFERENCE_USAGE")
        items = dedup_exact_duplicates([a])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == []

    def test_scripts_token_also_links(self) -> None:
        a = _f("运行 scripts/build.sh", "TOOL_REQUIREMENT", AuditVerdict.FAIL, AuditCategory.CONDITIONAL)
        b = _f("步骤 调 scripts/build.sh 打包", "HEADING_STEP", AuditVerdict.FAIL, AuditCategory.STEP)
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == ["#2"] and items[1].related == ["#1"]


class TestPostProcess:
    def test_compose_dedup_seq_link(self) -> None:
        # 编造×2(同文→合并 1,sources 两个);没读码表×2(跨类→保留互标)
        a1 = _f("禁止编造错误码", "CONSTRAINT", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        a2 = _f("禁止编造错误码", "ANTI_FABRICATION", AuditVerdict.FAIL, AuditCategory.PROHIBITION)
        b1 = _f("Use reference file: references/error-codes.md", "REFERENCE_USAGE",
                AuditVerdict.FAIL, AuditCategory.CONDITIONAL)
        b2 = _f("分类每个错误 — 读取 references/error-codes.md...", "HEADING_STEP",
                AuditVerdict.FAIL, AuditCategory.STEP)
        out = post_process_findings([a1, a2, b1, b2])
        assert len(out) == 3  # 4 → 3(编造合并)
        # 编造:#1、sources 两个、无 related
        fab = next(f for f in out if f.instruction_text == "禁止编造错误码")
        assert fab.seq == 1
        assert fab.sources == ["ANTI_FABRICATION", "CONSTRAINT"]
        assert fab.related == []
        # 没读码表两条互标
        cond = next(f for f in out if f.source == "REFERENCE_USAGE")
        step = next(f for f in out if f.source == "HEADING_STEP")
        assert cond.related == [f"#{step.seq}"]
        assert step.related == [f"#{cond.seq}"]


class TestWiringThroughAuditor:
    def test_audit_dedups_numbers_and_links(self, tmp_path, monkeypatch) -> None:
        """audit() 真的调了 post_process:用 _ok_json(all pass)让"NEVER fabricate"
        那句的 CONSTRAINT + ANTI_FABRICATION 两条都 PASS(同 text/cat/transcript/verdict)
        → 合并为 1、sources 含两者;每条有唯一 1..N seq。"""
        def fake(prompt, *a, **k):
            return json.dumps([
                {"index": i, "applicable": False, "violated": False,
                 "verdict": "pass", "evidence": "ok"}
                for i in range(20)
            ])
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        body = (
            "NEVER fabricate error codes or messages that do not appear in the log.\n\n"
            "1. Analyze the input carefully and read references/error-codes.md.\n"
        )
        tp = tmp_path / "u.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "user", "content": "x"}}) + "\n",
            encoding="utf-8",
        )
        report = TranscriptAuditor().audit("s", body, [tp])

        # 编造那句只一条(合并),sources 含 CONSTRAINT + ANTI_FABRICATION
        fab = [f for f in report.findings if "fabricat" in f.instruction_text.lower()]
        assert len(fab) == 1, [f.instruction_text for f in fab]
        assert set(fab[0].sources) == {"CONSTRAINT", "ANTI_FABRICATION"}

        # 每条唯一 1..N seq
        seqs = sorted(f.seq for f in report.findings)
        assert seqs == list(range(1, len(report.findings) + 1))

        # summary 与 findings 一致(去重后)
        assert report.summary["total"] == len(report.findings)

        # 无 (text, category, transcript, verdict) 完全重复(exact dup 全去重)
        keys = [(f.instruction_text, f.category, f.transcript, f.verdict) for f in report.findings]
        assert len(keys) == len(set(keys))
