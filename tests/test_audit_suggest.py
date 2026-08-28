"""tests for 审计→改进建议(--suggest 可选 LLM 后处理:suggest.py)。

覆盖:双门控(无 FAIL 不落盘 / UNRESOLVED 占比拦下)、逐指令簇归因映射
(fix 两类落位 / model_env 折叠计数 = Σ target_seqs / 不从漏引用反推)、wire 校验
(enum/category 缺 suggestion/无效 seq → dropped)、分批(>10 簇拆批,skill_body+PASS
每批全量)、prompt 内容(noise 标注 + per-FAIL seq/method + 聚合概要 + 单 transcript
退化纪律 + PASS 清单排除 derived 与回退链)、永不外抛(重试耗尽/意外异常/整批解析
失败)。CLI flag 与模板渲染的测试在 test_audit_cli.py / test_audit_viewer.py。
"""
from __future__ import annotations

import json

import pytest

from sift.audit import suggest as suggest_mod
from sift.audit.suggest import build_suggestions
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
    InstructionAggregate,
)


# ---- 构造 helper --------------------------------------------------------------


def _finding(
    text: str,
    verdict: AuditVerdict,
    *,
    seq: int = 0,
    transcript: str = "t1.jsonl",
    method: AuditMethod = AuditMethod.LLM,
    category: AuditCategory = AuditCategory.PROHIBITION,
    source: str = "CONSTRAINT",
    noise: str | None = None,
    synthetic: bool = False,
    derived: bool = False,
    evidence: str = "执行事实",
) -> AuditFinding:
    return AuditFinding(
        instruction_text=text, source=source, verdict=verdict, method=method,
        category=category, evidence=evidence, transcript=transcript, seq=seq,
        noise_label=noise, synthetic=synthetic, derived_from_transcript=derived,
    )


def _row(
    text: str,
    *,
    fail: int = 0,
    total: int = 1,
    fail_in: list[str] | None = None,
    unresolved: int = 0,
    headline: str = "",
    excerpt: str = "",
) -> InstructionAggregate:
    return InstructionAggregate(
        instruction_text=text, source="CONSTRAINT", category=AuditCategory.PROHIBITION,
        pass_count=max(total - fail - unresolved, 0), fail_count=fail,
        na_count=0, unresolved_count=unresolved, indeterminate_count=0, total=total,
        fail_rate=fail / total if total else 0.0,
        fail_in_transcripts=fail_in or [], headline=headline, source_excerpt=excerpt,
    )


def _report(
    findings: list[AuditFinding],
    rows: list[InstructionAggregate] | None = None,
    *,
    transcripts: list[str] | None = None,
    unresolved: int | None = None,
) -> AuditReport:
    transcripts = transcripts or ["t1.jsonl"]
    summary = {
        "total": len(findings),
        "pass": sum(1 for f in findings if f.verdict == AuditVerdict.PASS),
        "fail": sum(1 for f in findings if f.verdict == AuditVerdict.FAIL),
        "na": sum(1 for f in findings if f.verdict == AuditVerdict.NA),
        "unresolved": unresolved if unresolved is not None
        else sum(1 for f in findings if f.verdict == AuditVerdict.UNRESOLVED),
        "indeterminate": 0,
    }
    return AuditReport(
        skill_name="s", transcripts=transcripts, summary=summary,
        findings=findings, by_instruction=rows or [],
    )


def _wire(items: list[dict]) -> str:
    return json.dumps({"attributions": items}, ensure_ascii=False)


def _fake_llm(response: str, usage: dict | None = None, calls: list | None = None):
    """造 mock call_llm_with_retry:记 prompt、回 usage(带非数字键,守卫 isinstance)。"""
    _usage = usage if usage is not None else {"input_tokens": 10, "output_tokens": 5, "note": "x"}

    def fake(prompt, *a, **k):
        if calls is not None:
            calls.append(prompt)
        if on_usage := k.get("on_usage"):
            on_usage(dict(_usage))
        return response

    return fake


# ---- 门控 ----------------------------------------------------------------------


class TestGates:
    def test_no_fail_returns_none(self, monkeypatch) -> None:
        """全 PASS = 正常终态:不调 LLM、不落盘(None,--suggest 开也一样)。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            lambda *a, **k: pytest.fail("无 FAIL 不应调 LLM"),
        )
        report = _report([_finding("必须做 X", AuditVerdict.PASS)])
        assert build_suggestions(report, "skill 正文") is None

    def test_unresolved_gate_skips(self, monkeypatch) -> None:
        """UNRESOLVED 占比 > 30%(分母 = summary.total)→ 不跑 + skipped_reason。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            lambda *a, **k: pytest.fail("证据残缺不应调 LLM"),
        )
        # 2 FAIL + 2 UNRESOLVED → unresolved/total = 50% > 30%
        findings = [
            _finding("违规甲", AuditVerdict.FAIL, seq=1),
            _finding("违规乙", AuditVerdict.FAIL, seq=2),
            _finding("没判成一", AuditVerdict.UNRESOLVED, seq=3),
            _finding("没判成二", AuditVerdict.UNRESOLVED, seq=4),
        ]
        result = build_suggestions(_report(findings), "skill 正文")
        assert result is not None and result.skipped_reason
        assert "30%" in result.skipped_reason or "重跑" in result.skipped_reason
        assert result.suggestions == []


# ---- 归因映射与 wire 校验 -------------------------------------------------------


class TestAttributionMapping:
    def _fail_report(self) -> AuditReport:
        findings = [
            _finding("必须做 A", AuditVerdict.FAIL, seq=1, evidence="多会话没做 A"),
            _finding("必须做 A", AuditVerdict.FAIL, seq=2, transcript="t2.jsonl"),
            _finding("过时的步骤 B", AuditVerdict.FAIL, seq=3, category=AuditCategory.STEP,
                     source="HEADING_STEP", method=AuditMethod.PROGRAMMATIC),
            _finding("偶发失败 C", AuditVerdict.FAIL, seq=4),
        ]
        rows = [
            _row("必须做 A", fail=2, total=2, fail_in=["t1.jsonl", "t2.jsonl"]),
            _row("过时的步骤 B", fail=1, total=2, fail_in=["t1.jsonl"]),
            _row("偶发失败 C", fail=1, total=2, fail_in=["t1.jsonl"]),
        ]
        return _report(findings, rows, transcripts=["t1.jsonl", "t2.jsonl"])

    def test_three_categories(self, monkeypatch) -> None:
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "skill_fix", "target_seqs": [1, 2], "rationale": "系统性没做",
             "suggestion": "给 A 加显式约束", "keeps_pass": True, "keeps_pass_note": "不涉及 PASS 段"},
            {"category": "declaration_fix", "target_seqs": [3], "rationale": "声明过时",
             "suggestion": "把 B 改为可选", "keeps_pass": True},
            {"category": "model_env", "target_seqs": [4], "rationale": "单发工具噪声"},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.skipped_reason == ""
        assert [s.category for s in result.suggestions] == ["skill_fix", "declaration_fix"]
        assert result.suggestions[0].target_seqs == [1, 2]
        assert result.model_env_count == 1
        assert result.dropped_count == 0
        assert result.tokens == 15  # 非数字键(note)被守卫跳过

    def test_model_env_count_is_fail_count_not_entries(self, monkeypatch) -> None:
        """计数单位 = FAIL 条数(Σ target_seqs 去重),不是 attribution 条目数。"""
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "model_env", "target_seqs": [1, 2], "rationale": "噪声"},
            {"category": "model_env", "target_seqs": [3, 4], "rationale": "环境"},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.model_env_count == 4  # 2 条目 × 2 seq = 4 FAIL,不是 2

    def test_no_inference_from_unreferenced(self, monkeypatch) -> None:
        """未被建议引用的 FAIL 不反推进 model_env 计数(LLM 漏归因 ≠ 归因为模型)。"""
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "skill_fix", "target_seqs": [1], "rationale": "系统性",
             "suggestion": "改", "keeps_pass": True},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.model_env_count == 0  # #2/#3/#4 未被引用,不计

    def test_fix_without_suggestion_dropped(self, monkeypatch) -> None:
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "skill_fix", "target_seqs": [1], "rationale": "系统性"},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions == [] and result.dropped_count == 1

    def test_invalid_seqs_filtered(self, monkeypatch) -> None:
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "skill_fix", "target_seqs": [99, 1, True], "rationale": "r",
             "suggestion": "改", "keeps_pass": True},
            {"category": "model_env", "target_seqs": [999]},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions[0].target_seqs == [1]  # 越界/bool 剔除
        assert result.model_env_count == 0 and result.dropped_count == 1  # 全无效 → 丢条目

    def test_unknown_category_dropped(self, monkeypatch) -> None:
        """schema enum 正常会拦;代码侧防御(schema 外通道/畸形)照样丢弃。"""
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "model-environment", "target_seqs": [1], "rationale": "变体"},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.dropped_count == 1 and result.suggestions == []

    def test_keeps_pass_false_kept_for_display(self, monkeypatch) -> None:
        """keeps_pass=false → 建议照落(prompt 纪律不自纠,展示层诚实警示)。"""
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([
            {"category": "skill_fix", "target_seqs": [1], "rationale": "r",
             "suggestion": "改", "keeps_pass": False, "keeps_pass_note": "建议段落与某 PASS 指令相邻"},
        ])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions[0].keeps_pass is False

    def test_zero_attributions_is_normal(self, monkeypatch) -> None:
        """跑成但 LLM 零归因 → 正常态(非失败):reason 空、计数 0(渲染端不显)。"""
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([])))
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.skipped_reason == "" and result.suggestions == []
        assert result.model_env_count == 0 and result.dropped_count == 0


# ---- 分批与 prompt 内容 ---------------------------------------------------------


class TestPromptAndBatches:
    def test_batching_over_ten_clusters(self, monkeypatch) -> None:
        """12 簇 → 2 批;skill_body + PASS 清单每批全量,FAIL 簇不重叠。"""
        findings, rows = [], []
        for i in range(1, 13):
            text = f"违规指令{i}"
            findings.append(_finding(text, AuditVerdict.FAIL, seq=i))
            rows.append(_row(text, fail=1, total=1))
        findings.append(_finding("过着的指令", AuditVerdict.PASS, seq=13))
        rows.append(_row("过着的指令", fail=0, total=1))
        calls: list[str] = []
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=calls),
        )
        result = build_suggestions(_report(findings, rows), "SKILL 正文锚点", )
        assert result is not None
        assert len(calls) == 2
        for p in calls:
            assert "SKILL 正文锚点" in p
            assert "过着的指令" in p  # PASS 清单每批全量
        # 簇不重叠:每条违规指令恰出现在一个批 prompt 里
        for i in range(1, 13):
            assert sum(f"违规指令{i}\n" in p or f"违规指令{i} " in p for p in calls) == 1

    def test_prompt_payload(self, monkeypatch) -> None:
        """FAIL 载荷带 #N + method + noise 标注;聚合概要带 fail/total/in/unresolved/总数。"""
        calls: list[str] = []
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=calls),
        )
        findings = [
            _finding("系统性违规 X", AuditVerdict.FAIL, seq=1, noise="tool_noise",
                     method=AuditMethod.PROGRAMMATIC),
            _finding("偶发违规 Y", AuditVerdict.FAIL, seq=2, transcript="t2.jsonl"),
        ]
        rows = [
            _row("系统性违规 X", fail=1, total=2, fail_in=["t1.jsonl"], unresolved=1),
            _row("偶发违规 Y", fail=1, total=2, fail_in=["t2.jsonl"]),
        ]
        build_suggestions(_report(findings, rows, transcripts=["t1.jsonl", "t2.jsonl"]), "正文")
        p = calls[0]
        assert "#1" in p and "PROGRAMMATIC" in p
        assert "tool_noise" in p and "不得据其出 skill_fix" in p
        assert "失败 1/2" in p and "未判成 1" in p and "transcript 总数 2" in p
        assert "t1.jsonl" in p

    def test_single_transcript_degradation_note(self, monkeypatch) -> None:
        """单 transcript:每条 FAIL 都是 1/1,「单发」退出归因依据(写进 prompt)。"""
        prompts: list[str] = []
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=prompts))
        findings = [_finding("单会话违规", AuditVerdict.FAIL, seq=1)]
        build_suggestions(_report(findings), "正文")
        assert "单发失败" in prompts[0] and "不作为 model_env" in prompts[0]

    def test_pass_list_excludes_derived_and_falls_back(self, monkeypatch) -> None:
        """PASS 清单从 by_instruction 取(fail==0 且 pass>0):derived 行排除;
        headline 空回退 source_excerpt。"""
        calls: list[str] = []
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=calls))
        findings = [
            _finding("违规", AuditVerdict.FAIL, seq=1),
            _finding("正常指令甲", AuditVerdict.PASS, seq=2),
            _finding("反推来的读取", AuditVerdict.PASS, seq=3, derived=True),
        ]
        rows = [
            _row("正常指令甲", fail=0, total=1, headline=""),
            _row("反推来的读取", fail=0, total=1),  # derived 行(无标志,按 findings 反查排除)
        ]
        # source_excerpt 回退:headline 空时用 excerpt
        rows[0].source_excerpt = "正常指令甲的完整原文"
        build_suggestions(_report(findings, rows), "正文")
        p = calls[0]
        assert "正常指令甲的完整原文" in p  # headline 空 → source_excerpt 兜底
        assert "反推来的读取" not in p  # derived 不进保留账本

    def test_synthetic_cluster_own_entry(self, monkeypatch) -> None:
        """合成 finding(ORDER)无聚合行 → 逐条一簇,prompt 带合成注记。"""
        calls: list[str] = []
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=calls))
        findings = [
            _finding("(顺序)「A」须先于「B」", AuditVerdict.FAIL, seq=1,
                     synthetic=True, category=AuditCategory.STEP, source="HEADING_STEP"),
        ]
        build_suggestions(_report(findings, []), "正文")
        assert "合成关系声明" in calls[0]


# ---- 健壮性:永不外抛 -------------------------------------------------------------


class TestRobustness:
    def _fail_report(self) -> AuditReport:
        return _report([_finding("违规甲", AuditVerdict.FAIL, seq=1)])

    def test_retry_exhausted_skips(self, monkeypatch) -> None:
        from sift.concurrency import _LLMRetryExhausted

        def boom(*a, **k):
            raise _LLMRetryExhausted("LLM 调用重试耗尽(max_retries=4)")

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", boom)
        result = build_suggestions(self._fail_report(), "正文")
        assert result is not None and "重试耗尽" in result.skipped_reason

    def test_unexpected_exception_never_raises(self, monkeypatch) -> None:
        """catch-all:挂点在落盘前,任何异常都不得外抛(抛了毁整份报告)。"""
        def boom(*a, **k):
            raise ValueError("意外炸了")

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", boom)
        result = build_suggestions(self._fail_report(), "正文")
        assert result is not None
        assert "意外炸了" in result.skipped_reason

    def test_batch_parse_failure_counts_dropped(self, monkeypatch) -> None:
        """整批解析失败 → 该批簇计 dropped、其余批照跑(一批不毁全部)。"""
        responses = iter(["不是 JSON", _wire([
            {"category": "model_env", "target_seqs": [1], "rationale": "第二批归因"},
        ])])

        def fake(prompt, *a, **k):
            return next(responses)

        findings = [_finding(f"违规{i}", AuditVerdict.FAIL, seq=i) for i in range(1, 13)]
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        result = build_suggestions(_report(findings), "正文")
        assert result is not None
        assert result.skipped_reason == ""
        assert result.dropped_count == 10  # 第一批 10 簇整批没解出
        assert result.model_env_count == 1  # 第二批 wire 的 seq=1 是合法引用,照计(簇归簇)
