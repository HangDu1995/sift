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
        instruction_text=text,
        source=source,
        verdict=verdict,
        method=method,
        category=category,
        evidence=evidence,
        transcript=transcript,
        seq=seq,
        noise_label=noise,
        synthetic=synthetic,
        derived_from_transcript=derived,
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
        instruction_text=text,
        source="CONSTRAINT",
        category=AuditCategory.PROHIBITION,
        pass_count=max(total - fail - unresolved, 0),
        fail_count=fail,
        na_count=0,
        unresolved_count=unresolved,
        indeterminate_count=0,
        total=total,
        fail_rate=fail / total if total else 0.0,
        fail_in_transcripts=fail_in or [],
        headline=headline,
        source_excerpt=excerpt,
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
        "unresolved": unresolved
        if unresolved is not None
        else sum(1 for f in findings if f.verdict == AuditVerdict.UNRESOLVED),
        "indeterminate": 0,
    }
    return AuditReport(
        skill_name="s",
        transcripts=transcripts,
        summary=summary,
        findings=findings,
        by_instruction=rows or [],
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
    @staticmethod
    def test_no_fail_returns_none(monkeypatch) -> None:
        """全 PASS = 正常终态:不调 LLM、不落盘(None,--suggest 开也一样)。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            lambda *a, **k: pytest.fail("无 FAIL 不应调 LLM"),
        )
        report = _report([_finding("必须做 X", AuditVerdict.PASS)])
        assert build_suggestions(report, "skill 正文") is None

    @staticmethod
    def test_unresolved_gate_skips(monkeypatch) -> None:
        """UNRESOLVED 占比 > 30%(分母 = summary.total)→ 不跑 + skipped_reason。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            lambda *a, **k: pytest.fail("证据残缺不应调 LLM"),
        )
        # 两条失败、两条未判成；未判成占一半，超过三成的门槛。
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
    @staticmethod
    def _fail_report() -> AuditReport:
        findings = [
            _finding("必须做 A", AuditVerdict.FAIL, seq=1, evidence="多会话没做 A"),
            _finding("必须做 A", AuditVerdict.FAIL, seq=2, transcript="t2.jsonl"),
            _finding(
                "过时的步骤 B",
                AuditVerdict.FAIL,
                seq=3,
                category=AuditCategory.STEP,
                source="HEADING_STEP",
                method=AuditMethod.PROGRAMMATIC,
            ),
            _finding("偶发失败 C", AuditVerdict.FAIL, seq=4),
        ]
        rows = [
            _row("必须做 A", fail=2, total=2, fail_in=["t1.jsonl", "t2.jsonl"]),
            _row("过时的步骤 B", fail=1, total=2, fail_in=["t1.jsonl"]),
            _row("偶发失败 C", fail=1, total=2, fail_in=["t1.jsonl"]),
        ]
        return _report(findings, rows, transcripts=["t1.jsonl", "t2.jsonl"])

    def test_three_categories(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": [1, 2],
                            "rationale": "系统性没做",
                            "suggestion": "给 A 加显式约束",
                            "keeps_pass": True,
                            "keeps_pass_note": "不涉及 PASS 段",
                        },
                        {
                            "category": "declaration_fix",
                            "target_seqs": [3],
                            "rationale": "声明过时",
                            "suggestion": "把 B 改为可选",
                            "keeps_pass": True,
                        },
                        {
                            "category": "model_env",
                            "target_seqs": [4],
                            "rationale": "单发工具噪声",
                        },
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.skipped_reason == ""
        assert [s.category for s in result.suggestions] == [
            "skill_fix",
            "declaration_fix",
        ]
        assert result.suggestions[0].target_seqs == [1, 2]
        assert result.model_env_count == 1
        assert result.dropped_count == 0
        assert result.tokens == 15  # 非数字键(note)被守卫跳过

    def test_model_env_count_is_fail_count_not_entries(self, monkeypatch) -> None:
        """计数单位 = FAIL 条数(Σ target_seqs 去重),不是 attribution 条目数。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "model_env",
                            "target_seqs": [1, 2],
                            "rationale": "噪声",
                        },
                        {
                            "category": "model_env",
                            "target_seqs": [3, 4],
                            "rationale": "环境",
                        },
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.model_env_count == 4  # 2 条目 × 2 seq = 4 FAIL,不是 2

    def test_no_inference_from_unreferenced(self, monkeypatch) -> None:
        """未被建议引用的 FAIL 不反推进 model_env 计数(LLM 漏归因 ≠ 归因为模型)。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": [1],
                            "rationale": "系统性",
                            "suggestion": "改",
                            "keeps_pass": True,
                        },
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.model_env_count == 0  # #2/#3/#4 未被引用,不计

    def test_fix_without_suggestion_dropped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": [1],
                            "rationale": "系统性",
                        },
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions == [] and result.dropped_count == 1

    def test_invalid_seqs_filtered(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": [99, 1, True],
                            "rationale": "r",
                            "suggestion": "改",
                            "keeps_pass": True,
                        },
                        {"category": "model_env", "target_seqs": [999]},
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions[0].target_seqs == [1]  # 越界/bool 剔除
        assert result.model_env_count == 0 and result.dropped_count == 1  # 全无效 → 丢条目

    def test_non_list_target_seqs_dropped_not_raised(self, monkeypatch) -> None:
        """target_seqs 是裸标量(int/bool)→ 按无效丢条目,不得 TypeError 外抛。

        item 循环在 _call_batch 的 try 之外——裸标量在列表推导的 for 取迭代器这步
        就抛,会击穿 build_suggestions 的「永不向外抛」契约(挂点在落盘前)。
        """
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": 5,
                            "rationale": "r",
                            "suggestion": "改",
                        },
                        {"category": "model_env", "target_seqs": True},
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.suggestions == [] and result.dropped_count == 2

    def test_unknown_category_dropped(self, monkeypatch) -> None:
        """schema enum 正常会拦;代码侧防御(schema 外通道/畸形)照样丢弃。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "model-environment",
                            "target_seqs": [1],
                            "rationale": "变体",
                        },
                    ]
                )
            ),
        )
        result = build_suggestions(self._fail_report(), "skill 正文")
        assert result is not None
        assert result.dropped_count == 1 and result.suggestions == []

    def test_keeps_pass_false_kept_for_display(self, monkeypatch) -> None:
        """keeps_pass=false → 建议照落(prompt 纪律不自纠,展示层诚实警示)。"""
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(
                _wire(
                    [
                        {
                            "category": "skill_fix",
                            "target_seqs": [1],
                            "rationale": "r",
                            "suggestion": "改",
                            "keeps_pass": False,
                            "keeps_pass_note": "建议段落与某 PASS 指令相邻",
                        },
                    ]
                )
            ),
        )
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
    @staticmethod
    def test_batching_over_ten_clusters(monkeypatch) -> None:
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
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(_wire([]), calls=calls),
        )
        result = build_suggestions(
            _report(findings, rows),
            "SKILL 正文锚点",
        )
        assert result is not None
        assert len(calls) == 2
        for p in calls:
            assert "SKILL 正文锚点" in p
            assert "过着的指令" in p  # PASS 清单每批全量
        # 簇不重叠:每条违规指令恰出现在一个批 prompt 里
        for i in range(1, 13):
            assert sum(f"违规指令{i}\n" in p or f"违规指令{i} " in p for p in calls) == 1

    @staticmethod
    def test_prompt_payload(monkeypatch) -> None:
        """FAIL 载荷带 #N + method + noise 标注;聚合概要带 fail/total/in/unresolved/总数。"""
        calls: list[str] = []
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(_wire([]), calls=calls),
        )
        findings = [
            _finding(
                "系统性违规 X",
                AuditVerdict.FAIL,
                seq=1,
                noise="tool_noise",
                method=AuditMethod.PROGRAMMATIC,
            ),
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

    @staticmethod
    def test_single_transcript_degradation_note(monkeypatch) -> None:
        """单 transcript:每条 FAIL 都是 1/1,「单发」退出归因依据(写进 prompt)。"""
        prompts: list[str] = []
        monkeypatch.setattr(
            "sift.audit.suggest.call_llm_with_retry",
            _fake_llm(_wire([]), calls=prompts),
        )
        findings = [_finding("单会话违规", AuditVerdict.FAIL, seq=1)]
        build_suggestions(_report(findings), "正文")
        assert "单发失败" in prompts[0] and "不作为 model_env" in prompts[0]

    @staticmethod
    def test_pass_list_excludes_derived_and_falls_back(monkeypatch) -> None:
        """PASS 清单从 by_instruction 取(fail==0 且 pass>0):derived 行排除;
        headline 空回退 source_excerpt。
        """
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

    @staticmethod
    def test_synthetic_cluster_own_entry(monkeypatch) -> None:
        """合成 finding(ORDER)无聚合行 → 逐条一簇,prompt 带合成注记。"""
        calls: list[str] = []
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", _fake_llm(_wire([]), calls=calls))
        findings = [
            _finding(
                "(顺序)「A」须先于「B」",
                AuditVerdict.FAIL,
                seq=1,
                synthetic=True,
                category=AuditCategory.STEP,
                source="HEADING_STEP",
            ),
        ]
        build_suggestions(_report(findings, []), "正文")
        assert "合成关系声明" in calls[0]


# ---- 健壮性:永不外抛 -------------------------------------------------------------


class TestRobustness:
    @staticmethod
    def test_batch_parse_failure_counts_dropped(monkeypatch) -> None:
        """整批解析失败 → 该批簇计 dropped、其余批照跑(一批不毁全部)。
        分批并行后调用序不定,fake 按 prompt 内容分流(第二批 prompt 含「违规11」)。
        """

        def fake(prompt, *a, **k):
            if "#11" in prompt:
                return _wire(
                    [
                        {
                            "category": "model_env",
                            "target_seqs": [1],
                            "rationale": "第二批归因",
                        }
                    ]
                )
            return "不是 JSON"

        findings = [_finding(f"违规{i}", AuditVerdict.FAIL, seq=i) for i in range(1, 13)]
        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        result = build_suggestions(_report(findings), "正文")
        assert result is not None
        assert result.skipped_reason == ""
        assert result.dropped_count == 10  # 第一批(违规1..10)10 簇整批没解出
        assert result.model_env_count == 1  # 第二批 wire 的 seq=1 是合法引用,照计(簇归簇)

    @staticmethod
    def _fail_report() -> AuditReport:
        return _report([_finding("违规甲", AuditVerdict.FAIL, seq=1)])

    def test_retry_exhausted_skips(self, monkeypatch) -> None:
        from sift.concurrency import LLMRetryExhausted

        def boom(*a, **k):
            raise LLMRetryExhausted("LLM 调用重试耗尽(max_retries=4)")

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


# ---- 弹性上限 + 分批并行 ----------------------------------------------------------


class TestElasticParallel:
    """首档输出上限按批内簇数弹性给(固定小帽对深度归因必截断)、批间并行、单批挂不毁其余批。

    探针锚点(glm-5.3 直连,2026-08-28):6 簇 → output 10.2k,12 簇 → 16.2k
    (≈ 4k 底噪 + 1.0k/簇);首档公式 6000 + 2500×簇数从宽覆盖(见 suggest.py 常量)。
    """

    @staticmethod
    def _n_fail_report(n: int) -> AuditReport:
        return _report([_finding(f"违规{i}", AuditVerdict.FAIL, seq=i) for i in range(1, n + 1)])

    def test_max_tokens_scales_with_batch_clusters(self, monkeypatch) -> None:
        """max_tokens = 6000 + 2500×批内簇数(从宽:过量给免费、给窄撞方差浪费一整趟截断);
        超 10 簇分批后各批按各自己算。
        """
        captured: list[int | None] = []

        def fake(prompt, *a, **k):
            captured.append(k.get("max_tokens"))
            return _wire([{"category": "model_env", "target_seqs": [1], "rationale": "r"}])

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        build_suggestions(self._n_fail_report(3), "正文")
        assert captured == [6000 + 2500 * 3]  # 单批:3 簇
        captured.clear()
        build_suggestions(self._n_fail_report(12), "正文")  # 两批:10 + 2
        assert sorted(captured) == sorted([6000 + 2500 * 10, 6000 + 2500 * 2])

    def test_batches_merge_and_usage_summed(self, monkeypatch) -> None:
        """两批并行各自调 LLM;建议按簇序合并,usage 并发回报不丢(锁累加,tokens=两批之和)。"""

        def fake(prompt, *a, **k):
            if on_usage := k.get("on_usage"):
                on_usage({"input_tokens": 10, "output_tokens": 5, "note": "x"})
            if "#11" in prompt:  # 第二批(违规11/12)
                return _wire(
                    [
                        {
                            "category": "model_env",
                            "target_seqs": [12],
                            "rationale": "第二批",
                        }
                    ]
                )
            return _wire(
                [
                    {
                        "category": "skill_fix",
                        "target_seqs": [1],
                        "rationale": "第一批",
                        "suggestion": "改一改",
                    }
                ]
            )

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        result = build_suggestions(self._n_fail_report(12), "正文")
        assert result is not None
        assert [(s.category, s.target_seqs) for s in result.suggestions] == [("skill_fix", [1])]
        assert result.model_env_count == 1
        assert result.tokens == 2 * 15  # 两批并发回报,锁防覆盖丢计数
        assert result.input_tokens == 2 * 10  # 分项同锁累加(阶段表 suggest 行用)
        assert result.output_tokens == 2 * 5
        assert result.calls == 2  # 每次响应计 1,与判官阶段 calls 同口径

    def test_one_batch_exhausted_keeps_other(self, monkeypatch) -> None:
        """单批重试耗尽:skipped_reason 收尾,其余批建议保留,挂掉批的簇计 dropped。"""
        from sift.concurrency import LLMRetryExhausted

        def fake(prompt, *a, **k):
            if "#11" in prompt:
                raise LLMRetryExhausted("LLM 调用重试耗尽(max_retries=4)")
            return _wire(
                [
                    {
                        "category": "skill_fix",
                        "target_seqs": [1],
                        "rationale": "r",
                        "suggestion": "s",
                    }
                ]
            )

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        result = build_suggestions(self._n_fail_report(12), "正文")
        assert result is not None
        assert "重试耗尽" in result.skipped_reason
        assert [(s.category, s.target_seqs) for s in result.suggestions] == [("skill_fix", [1])]
        assert result.dropped_count == 2  # 挂掉那批的 2 簇

    def test_progress_announces_phase_and_batches(self, monkeypatch) -> None:
        """判官→汇总行之间的静默期先报阶段头(「生成改进建议:归因 N 簇…」——判官
        完成后一静默数分钟不知道在干嘛);多批时各批完成/失败打批级行(看得出最慢
        一批),单批不打批级行(CLI 汇总行收口,不叠噪声)。
        """
        from sift.concurrency import LLMRetryExhausted

        def fake(prompt, *a, **k):
            if "#11" in prompt:  # 第二批(违规11/12)
                raise LLMRetryExhausted("LLM 调用重试耗尽(max_retries=4)")
            return _wire([{"category": "model_env", "target_seqs": [1], "rationale": "r"}])

        monkeypatch.setattr("sift.audit.suggest.call_llm_with_retry", fake)
        lines: list[str] = []
        result = build_suggestions(self._n_fail_report(12), "正文", on_progress=lines.append)
        assert result is not None
        assert any("生成改进建议" in l and "12 个失败簇" in l and "2 批并行" in l for l in lines)
        done = [l for l in lines if "完成 ⏱" in l]
        fail = [l for l in lines if "失败:重试耗尽" in l]
        assert len(done) == 1 and "[批 1/2]" in done[0]
        assert len(fail) == 1 and "[批 2/2]" in fail[0]

        lines.clear()
        build_suggestions(self._n_fail_report(6), "正文", on_progress=lines.append)
        assert any("生成改进建议" in l and "1 批" in l for l in lines)
        assert not [l for l in lines if "[批" in l]  # 单批无批级行
