"""tests/test_audit_dedup.py — findings 去重(Case A 同文)+ 编号 + 关联(Case B 跨类同行为)。

audit 的 findings 平铺里有两类"看着多个、其实是一个"的情况:
- Case A 同文重复:同一句被 extractor 抽成 CONSTRAINT + ANTI_FABRICATION,同类别
  (prohibition)、同 verdict → 合并成一条,sources 记两个来源。
- Case B 同行为跨类:同一行为(如没读 error-codes.md)同时违反 条件性 + 步骤 两条
  *不同* 规则 → 不合并(确实是两条规则),但互标 related=["#N"],报告里 #N 可定位。
- Case C 同源矛盾:同一声明行被禁令/步骤两镜头同 excerpt 双抽,两判官判出 PASS 与
  FAIL → 互标 conflict_with=["#N"](标注不仲裁),warnings 报矛盾组数。

去重后给每条编 #1..#N(related/conflict_with 引用序号、报告可读)。post_process_findings
串三步,audit() 在 transcript 循环后调它(unverified/summary/by_instruction 都基于去重后
findings);annotate_conflicts 在 noise 标注旁单独调(返摘要供 warnings)。
"""

from __future__ import annotations

import json

from finding_fixtures import FindingEvidence, FindingOrigin
from sift.audit.auditor import TranscriptAuditor
from sift.audit.dedup import (
    annotate_conflicts,
    assign_sequence,
    dedup_exact_duplicates,
    link_related,
    post_process_findings,
)
from sift.audit.judge_context import AuditOptions
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditVerdict,
)


def _f(
    text: str,
    verdict: AuditVerdict = AuditVerdict.PASS,
    transcript: str = "t.jsonl",
    origin: FindingOrigin = FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
    result: FindingEvidence = FindingEvidence(),
) -> AuditFinding:
    source = origin.source
    category = origin.category
    derived = origin.derived
    excerpt = origin.excerpt
    synthetic = origin.synthetic
    method = result.method
    evidence = result.text
    return AuditFinding(
        instruction_text=text,
        source=source,
        verdict=verdict,
        category=category,
        evidence=evidence,
        method=method,
        transcript=transcript,
        derived_from_transcript=derived,
        source_excerpt=excerpt,
        synthetic=synthetic,
    )


class TestCaseADedup:
    @staticmethod
    def test_merges_same_text_category_transcript_verdict() -> None:
        a = _f(
            text="禁止编造",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b = _f(
            text="禁止编造",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 1
        assert out[0].sources == ["ANTI_FABRICATION", "CONSTRAINT"]  # 排序去重

    @staticmethod
    def test_keeps_when_verdict_differs() -> None:
        a = _f(
            text="禁止编造",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b = _f(
            text="禁止编造",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 2  # verdict 不同 → 都留(不掩盖矛盾)

    @staticmethod
    def test_keeps_across_transcripts() -> None:
        a = _f(
            text="禁止编造",
            verdict=AuditVerdict.FAIL,
            transcript="t1",
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b = _f(
            text="禁止编造",
            verdict=AuditVerdict.FAIL,
            transcript="t2",
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        out = dedup_exact_duplicates([a, b])
        assert len(out) == 2  # 跨 transcript 不合并(不同会话)

    @staticmethod
    def test_singleton_populates_sources() -> None:
        a = _f(
            text="读 X",
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        out = dedup_exact_duplicates([a])
        assert out[0].sources == ["REFERENCE_USAGE"]

    @staticmethod
    def test_preserves_order() -> None:
        items = [
            _f(
                text=f"t{i}",
                origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            )
            for i in range(3)
        ]
        out = dedup_exact_duplicates(items)
        assert [f.instruction_text for f in out] == ["t0", "t1", "t2"]

    @staticmethod
    def test_three_way_merge() -> None:
        # 极端:三个来源同文同 verdict → 合一,sources 三者
        a = _f(
            text="x",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b = _f(
            text="x",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        c = _f(
            text="x",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )  # 同 source 再来一条
        out = dedup_exact_duplicates([a, b, c])
        assert len(out) == 1
        assert out[0].sources == ["ANTI_FABRICATION", "CONSTRAINT"]  # source 去重


class TestSequence:
    @staticmethod
    def test_assigns_1_based_in_order() -> None:
        items = [
            _f(
                text="a",
                origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            ),
            _f(
                text="b",
                origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            ),
            _f(
                text="c",
                origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            ),
        ]
        assign_sequence(items)
        assert [f.seq for f in items] == [1, 2, 3]


class TestCaseBLink:
    @staticmethod
    def test_links_by_shared_ref_token() -> None:
        a = _f(
            text="Use reference file: references/error-codes.md",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="分类每个错误 — 读取 references/error-codes.md,映射到已知码。",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)  # a=#1, b=#2
        link_related(items)
        assert items[0].related == ["#2"]
        assert items[1].related == ["#1"]

    @staticmethod
    def test_no_link_without_shared_token() -> None:
        a = _f(
            text="禁止编造错误码",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b = _f(
            text="写报告 error-report.json",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == [] and items[1].related == []

    @staticmethod
    def test_no_link_across_transcripts() -> None:
        a = _f(
            text="读 references/x.md",
            transcript="t1",
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="步骤 读取 references/x.md",
            transcript="t2",
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == [] and items[1].related == []

    @staticmethod
    def test_links_any_verdict() -> None:
        # 都标:FAIL + PASS 都关联(不只 FAIL)
        a = _f(
            text="读 references/x.md",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="步骤 读 references/x.md",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == ["#2"] and items[1].related == ["#1"]

    @staticmethod
    def test_no_self_link() -> None:
        a = _f(
            text="读 references/x.md",
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == []

    @staticmethod
    def test_scripts_token_also_links() -> None:
        a = _f(
            text="运行 scripts/build.sh",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="TOOL_REQUIREMENT", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="步骤 调 scripts/build.sh 打包",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
        items = dedup_exact_duplicates([a, b])
        assign_sequence(items)
        link_related(items)
        assert items[0].related == ["#2"] and items[1].related == ["#1"]


class TestPostProcess:
    @staticmethod
    def test_compose_dedup_seq_link() -> None:
        # 编造×2(同文→合并 1,sources 两个);没读码表×2(跨类→保留互标)
        a1 = _f(
            text="禁止编造错误码",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        a2 = _f(
            text="禁止编造错误码",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(),
        )
        b1 = _f(
            text="Use reference file: references/error-codes.md",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b2 = _f(
            text="分类每个错误 — 读取 references/error-codes.md...",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
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


class TestCaseCConflicts:
    """同源矛盾配对:同 (transcript, source_excerpt) 组内 PASS 与 FAIL 并存 → 互标
    conflict_with。标注不仲裁——不翻判、不合并(Case A 有意保留矛盾 verdict 的信号,
    Case C 只负责让矛盾可见)。摘要是 warnings 的例对来源。
    """

    @staticmethod
    def test_mutual_annotation_cross_category() -> None:
        a = _f(
            text="禁止用 Bash 探查目录**",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(
                source="CONSTRAINT", category=AuditCategory.PROHIBITION, excerpt="禁止用 Bash 探查目录。"
            ),
            result=FindingEvidence(),
        )
        b = _f(
            text="禁止用 Bash 探查目录",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP, excerpt="禁止用 Bash 探查目录。"),
            result=FindingEvidence(),
        )
        assign_sequence([a, b])
        summaries = annotate_conflicts([a, b])
        assert summaries == ["#1↔#2"]
        assert a.conflict_with == ["#2"]  # PASS 侧指 FAIL 侧
        assert b.conflict_with == ["#1"]

    @staticmethod
    def test_same_category_opposition_also_annotated() -> None:
        # 同类别内的 verdict 相反同样矛盾(不限跨镜头)
        a = _f(
            text="读 STATE",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP, excerpt="读 STATE"),
            result=FindingEvidence(),
        )
        b = _f(
            text="必须读 STATE",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.STEP, excerpt="读 STATE"),
            result=FindingEvidence(),
        )
        assign_sequence([a, b])
        assert annotate_conflicts([a, b]) == ["#1↔#2"]

    @staticmethod
    def test_multi_member_group_summary_and_links() -> None:
        p1 = _f(
            text="a",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        p2 = _f(
            text="b",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        f1 = _f(
            text="c",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        assign_sequence([p1, p2, f1])
        assert annotate_conflicts([p1, p2, f1]) == ["#1/#2↔#3"]
        assert p1.conflict_with == ["#3"] and p2.conflict_with == ["#3"]
        assert f1.conflict_with == ["#1", "#2"]

    @staticmethod
    def test_no_opposition_no_annotation() -> None:
        # PASS+PASS / FAIL+N/A / FAIL+UNRESOLVED / FAIL+INDETERMINATE 都不是对立面
        cases = [
            (AuditVerdict.PASS, AuditVerdict.PASS),
            (AuditVerdict.FAIL, AuditVerdict.NA),
            (AuditVerdict.FAIL, AuditVerdict.UNRESOLVED),
            (AuditVerdict.FAIL, AuditVerdict.INDETERMINATE),
        ]
        for v1, v2 in cases:
            a = _f(
                text="x",
                verdict=v1,
                origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            )
            b = _f(
                text="y",
                verdict=v2,
                origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
                result=FindingEvidence(),
            )
            assign_sequence([a, b])
            assert annotate_conflicts([a, b]) == []
            assert a.conflict_with == [] and b.conflict_with == []

    @staticmethod
    def test_isolated_across_transcripts() -> None:
        # 同 excerpt 不同会话:各自成立,不算矛盾
        a = _f(
            text="x",
            verdict=AuditVerdict.PASS,
            transcript="t1",
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="y",
            verdict=AuditVerdict.FAIL,
            transcript="t2",
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        assign_sequence([a, b])
        assert annotate_conflicts([a, b]) == []

    @staticmethod
    def test_skips_empty_excerpt_and_synthetic() -> None:
        a = _f(
            text="x",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )  # excerpt 空
        b = _f(
            text="y",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        s = _f(
            text="z",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(
                excerpt="同源行", synthetic=True, source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL
            ),
            result=FindingEvidence(),
        )
        c = _f(
            text="w",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        assign_sequence([a, b, s, c])
        assert annotate_conflicts([a, b, s, c]) == []
        assert c.conflict_with == []  # 对端是 synthetic → 不参与,无对立面

    @staticmethod
    def test_excerpt_whitespace_normalized() -> None:
        # excerpt 首尾空白剥离后同组(source_excerpt 是归一引文,尾空白不该拆组)
        a = _f(
            text="x",
            verdict=AuditVerdict.PASS,
            origin=FindingOrigin(excerpt="同源行", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        b = _f(
            text="y",
            verdict=AuditVerdict.FAIL,
            origin=FindingOrigin(excerpt="  同源行  ", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(),
        )
        assign_sequence([a, b])
        assert annotate_conflicts([a, b]) == ["#1↔#2"]


class TestWiringThroughAuditor:
    @staticmethod
    def test_audit_dedups_numbers_and_links(tmp_path, monkeypatch) -> None:
        """audit() 真的调了 post_process:用 _ok_json(all pass)让"NEVER fabricate"
        那句的 CONSTRAINT + ANTI_FABRICATION 两条都 PASS(同 text/cat/transcript/verdict)
        → 合并为 1、sources 含两者;每条有唯一 1..N seq。
        """

        def fake(prompt, *a, **k):
            return json.dumps(
                [
                    {"index": i, "applicable": False, "violated": False, "verdict": "pass", "evidence": "ok"}
                    for i in range(20)
                ]
            )

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
        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

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

    @staticmethod
    def test_audit_flags_same_source_conflict(tmp_path, monkeypatch) -> None:
        """audit() 接线 Case C:同一声明行被禁令镜头(CONSTRAINT)与步骤镜头
        (HEADING_STEP)双抽、同 source_excerpt,两判官判出 PASS 与 FAIL →
        findings 互标 conflict_with + warnings 报「同源矛盾」(裸奔矛盾对最伤报告可信度)。
        fake 按各判官 prompt 的独有响应键区分:条件性问 applicable、禁令问 violated、
        步骤问 verdict。禁令全 PASS、步骤全 FAIL → 唯一同 excerpt 双抽的行成矛盾对。
        """

        def fake(prompt, *a, **k):
            n = 20
            if "applicable" in prompt:  # 条件性批:全不适用
                rows = [{"index": i, "applicable": False, "reason": "不涉及"} for i in range(n)]
            elif "violated" in prompt:  # 禁令批:全未违反(PASS)
                rows = [{"index": i, "violated": False, "evidence": "ok"} for i in range(n)]
            else:  # 步骤批(及 refine/headline 等,非判定路径容忍废响应):全 fail
                rows = [{"index": i, "verdict": "fail", "evidence": "没走这一步"} for i in range(n)]
            return json.dumps(rows)

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        body = (
            "NEVER fabricate error codes or messages that do not appear in the log.\n\n"
            "## 执行规约\n\n"
            "1. 禁止 direct Bash execution for any real operation。\n"
            "2. Analyze the input carefully and read references/error-codes.md.\n"
        )
        tp = tmp_path / "u.jsonl"
        tp.write_text(
            json.dumps({"message": {"role": "user", "content": "x"}}) + "\n",
            encoding="utf-8",
        )
        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

        # 同一行的两条:禁令 PASS + 步骤 FAIL,互标 conflict_with
        pair = [f for f in report.findings if "direct Bash" in f.instruction_text]
        assert len(pair) == 2, [(f.category, f.verdict) for f in pair]
        by_verdict = {f.verdict: f for f in pair}
        assert by_verdict[AuditVerdict.FAIL].conflict_with == [f"#{by_verdict[AuditVerdict.PASS].seq}"]
        assert by_verdict[AuditVerdict.PASS].conflict_with == [f"#{by_verdict[AuditVerdict.FAIL].seq}"]

        # warnings 报矛盾组数 + 例对
        assert any("同源矛盾 1 组" in w for w in report.warnings), report.warnings
