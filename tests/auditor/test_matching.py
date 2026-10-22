"""Auditor matching behavior tests."""

from __future__ import annotations

import json
import re

import pytest

from sift.audit.auditor import (
    _LLM_RETRY_MAX,
    TranscriptAuditor,
)
from sift.audit.judge_context import JudgeCallOptions, JudgeTranscript
from sift.models import ToolCallRecord
from sift.models.audit_models import (
    AuditCategory,
    AuditMethod,
    AuditVerdict,
)

from .helpers import _ref_inst, _tc, _tool_inst


class TestMatchedNoLlm:
    @staticmethod
    def test_tool_match_pass_and_no_call(monkeypatch) -> None:
        """命中 tool_name → PASS,evidence 中文,method=PROGRAMMATIC,call_claude 未被调用。"""
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        trace = [_tc("Bash", "echo hi")]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_tool_inst(tool_name="Bash")],
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="t1.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.PROGRAMMATIC
        assert f.transcript == "t1.jsonl"
        assert f.evidence == "trace 中调用了 Bash"
        assert f.source == "TOOL_REQUIREMENT"
        assert calls["n"] == 0  # 命中即 PASS,绝不调 LLM

    @staticmethod
    def test_script_path_match_pass(monkeypatch) -> None:
        """命中 script_path → PASS,evidence "trace 中读取了 {path}"。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: pytest.fail("不该调 LLM"))
        transcript_auditor = TranscriptAuditor()
        trace = [_tc("Read", "cat design.md")]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_ref_inst(script_path="design.md")],
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="t2.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence == "trace 中读取了 design.md"
        assert findings[0].source == "REFERENCE_USAGE"

    @staticmethod
    def test_section_passthrough_source_section_wins(monkeypatch) -> None:
        """finding.section 透传:source_section(真标题回填)优先,缺省回退 section。

        覆盖三条构造路径:命中 PASS 工厂(_make_match_pass_finding)、LLM 失败
        UNRESOLVED 工厂(_make_unresolved_finding)——声明分组标题在一切 verdict 路径
        上都跟指令走(覆盖视图分组键)。
        """
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: pytest.fail("不该调 LLM"))
        transcript_auditor = TranscriptAuditor()
        trace = [_tc("Bash", "echo hi")]

        # 回退路径:source_section 空 → 用 section 伪节名/真标题
        plain = _tool_inst(tool_name="Bash")
        f_plain = transcript_auditor.engine.judge_conditional(
            instructions=[plain],
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="t1.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f_plain.section == "Tools"

        # 回填路径:source_section 有值 → 覆盖 section
        backfilled = _tool_inst(tool_name="Bash")
        backfilled.source_section = "安全红线"
        f_back = transcript_auditor.engine.judge_conditional(
            instructions=[backfilled],
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="t1.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f_back.section == "安全红线"

        # UNRESOLVED 工厂同样透传(不命中 trace + LLM 抛错 → 没判成)
        def boom(*a, **k):
            raise RuntimeError("LLM 挂了")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        f_unres = transcript_auditor.engine.judge_conditional(
            instructions=[backfilled],
            evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t1.jsonl"),
            call_options=JudgeCallOptions(),
        )[0]
        assert f_unres.verdict == AuditVerdict.UNRESOLVED
        assert f_unres.section == "安全红线"


class TestUnmatchedLlm:
    @staticmethod
    def test_applicable_true_is_fail(monkeypatch) -> None:
        """未命中 + LLM applicable=true → FAIL(该做没做),method=LLM。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": true}]',
        )
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.CONDITIONAL
        assert f.transcript == "t.jsonl"
        assert f.method == AuditMethod.LLM
        assert f.evidence.startswith("场景应触发")
        assert "Bash" in f.evidence

    @staticmethod
    def test_applicable_false_is_na(monkeypatch) -> None:
        """未命中 + LLM applicable=false(带 reason)→ N/A,evidence 用 LLM 给的具体理由(非套话)。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false, "reason": "本任务只生成 spec,未跑测试"}]',
        )
        transcript_auditor = TranscriptAuditor()

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_ref_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.NA
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.LLM
        assert "本任务只生成 spec,未跑测试" in f.evidence  # LLM 给的具体理由,非硬编码套话

    @staticmethod
    def test_applicable_false_without_reason_keeps_na_with_fallback(monkeypatch) -> None:
        """applicable=false 但 LLM 未给 reason → 仍 N/A,evidence 退兜底套话(不降级,对齐 step)。

        与 indeterminate 滥用闸不同:N/A 是合法高频判决,空 reason **不**降级 UNRESOLVED,
        只退兜底 evidence——镜像 step judge 的 ``ev or "场景不涉及该步"``(捕获+用,非惩罚闸)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        transcript_auditor = TranscriptAuditor()

        findings = transcript_auditor.engine.judge_conditional(
            instructions=[_ref_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.NA  # 空 reason 不降级,仍 N/A
        assert "场景不涉及" in f.evidence  # 退兜底套话

    @staticmethod
    def test_runtime_error_unverifiable_na(monkeypatch) -> None:
        """call_claude 持续抛 RuntimeError(重试耗尽)→ 标 N/A(宁少报不乱报)。"""
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 不报 FAIL,标 UNRESOLVED(没判成)
        assert f.unresolved_cause == "unknown"  # "claude CLI down" 未命中模式 → unknown
        assert f.category == AuditCategory.CONDITIONAL
        assert f.method == AuditMethod.LLM  # 低置信度(判不准兜底,非 LLM 明确判 N/A)
        assert "无法确证" in f.evidence
        # 重试耗尽:1 次首发 + _LLM_RETRY_MAX 次重试,全失败才兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX

    @staticmethod
    def test_parse_failure_unverifiable_na(monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 标 N/A(宁少报不乱报)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: "我觉得都不该触发")
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert "无法确证" in f.evidence
        assert f.method == AuditMethod.LLM

    @staticmethod
    def test_missing_applicable_key_is_unverifiable_na(monkeypatch) -> None:
        """条目有 index 但缺 applicable 键 → 标 N/A(宁少报不乱报)。

        与 I1 当初相反:I1 担心"缺键被洗成 N/A → 漏报违规"故保守 FAIL;但用户原则
        '宁可少报不能乱报'推翻之——LLM 没给明确 applicable = 判不准 = N/A,绝不报假 FAIL。
        严格 ``is True/is False`` 判断保留(畸形值不洗成明确 N/A,仍走判不准兜底 N/A)。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0}]',
        )
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报 FAIL
        assert f.method == AuditMethod.LLM  # 低置信度(判不准兜底)
        assert "无法确证" in f.evidence

    @staticmethod
    def test_string_applicable_value_is_unverifiable_na(monkeypatch) -> None:
        """applicable 是字符串 "false"(非布尔)→ 标 N/A(宁少报不乱报)。

        回归 M1:旧实现 ``bool("false")`` = True(非空串恒真)会洗畸形值。现保留严格
        ``is True/is False`` 判断(畸形非布尔值不当明确结论),落到判不准兜底 → N/A。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": "false"}]',
        )
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成 → UNRESOLVED,不报 FAIL
        assert f.method == AuditMethod.LLM
        assert "无法确证" in f.evidence

    @staticmethod
    def test_applicable_prompt_has_trace_and_window_rule(monkeypatch) -> None:
        """未命中交 LLM 时,prompt 必须带工具调用记录 + 「窗口外不判应触发」规则。

        2026-08-30 误报修复的守卫:条件性判官原只看场景描述、看不到记录止于何处——
        场景文本提过整个计划,就对「系于从未开始阶段的指令」一律 applicable=true →
        trace 未调用 → 假 FAIL(真跑暴露:验收标准段里的标识符会被抽成工具要求)。
        补 tool_trace 可见性 + 窗口规则(含反向闸:已开始阶段不得借窗口洗掉)。
        """
        prompts: list[str] = []

        def fake(prompt, *a, **k):
            prompts.append(prompt)
            return json.dumps(
                [
                    {
                        "index": 0,
                        "applicable": False,
                        "reason": "记录止于 Task 32 验收,Task 37 从未开始",
                    }
                ]
            )

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        trace = [ToolCallRecord(name="Task", input_summary="Verify Task 32 - Branch-1 KernelST")]

        TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst(text="调用 FooFunc", tool_name="FooFunc")],
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="w.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert len(prompts) == 1
        assert "实际工具调用记录" in prompts[0]
        assert "Verify Task 32" in prompts[0]  # trace 内容真进了 prompt(不是空段)
        assert "从未开始" in prompts[0]  # 窗口规则
        assert "不判应触发" in prompts[0]
        assert "不得借「记录窗口」" in prompts[0]  # 反向闸


class TestRetryRecovery:
    """整批 transient 失败时重试救回:前几次 RuntimeError、后续成功 → 正常判定,不兜底 N/A。

    回归 audit-out3:2 个整批(25 条)撞限流直接放弃 → N/A。重试后 transient 失败大多
    能救回,真判(给出 resolved verdict)而非兜底。
    """

    @staticmethod
    def test_retry_recovers_then_judges_normally(monkeypatch) -> None:
        """call_claude 前 _LLM_RETRY_MAX 次 RuntimeError、第 3 次成功 → 重试救回,resolved N/A。"""
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= _LLM_RETRY_MAX:  # 前 _LLM_RETRY_MAX 次 transient 失败
                raise RuntimeError("transient rate limit")
            return '[{"index": 0, "applicable": false}]'  # 第 3 次:N/A resolved

        monkeypatch.setattr("sift.audit.auditor.call_claude", flaky)
        transcript_auditor = TranscriptAuditor()

        findings = _judge_missing_tool(transcript_auditor)

        f = findings[0]
        assert f.verdict == AuditVerdict.NA  # applicable=false → N/A
        # 关键:resolved(0.8)而非兜底(0.3)→ 重试成功救回,真判了
        assert f.method == AuditMethod.LLM
        assert "场景不涉及" in f.evidence  # LLM 给的 evidence,非"无法确证"兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX  # 重试 _LLM_RETRY_MAX 次后第 3 次成功

    @staticmethod
    def test_429_backoff_is_exponential(monkeypatch) -> None:
        """429 连撞 → 退避指数递增(2/4/8/16s,基数×2^attempt)。

        速率限制按分钟窗口计,固定 2s 退避在窗口内反复撞死(glm-5.3 实测 8 并发 3 个
        秒 429);指数退避让连撞的等待跨过窗口。抖动置 0 断言精确序列。
        """
        import sift.audit.auditor as aud_mod
        from sift.llm_transport import HttpRateLimited

        sleeps: list[float] = []

        def always_429(*a, **k):
            raise HttpRateLimited("裸 HTTP 429(账户速率/并发限制)")

        monkeypatch.setattr("sift.audit.auditor.call_claude", always_429)
        monkeypatch.setattr(aud_mod.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(aud_mod.random, "uniform", lambda a, b: 0.0)

        findings = TranscriptAuditor().engine.judge_conditional(
            instructions=[_tool_inst()],
            evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert findings[0].verdict == AuditVerdict.UNRESOLVED  # 耗尽 → 没判成
        # 429 专属退避(except 分支):基数 × 2^attempt → 2/4/8/16(attempt 0 起)
        throttle_waits = [s for s in sleeps if s in (2.0, 4.0, 8.0, 16.0)]
        assert throttle_waits == [2.0, 4.0, 8.0, 16.0]


class TestMixed:
    @staticmethod
    def test_match_and_unmixed_verdicts(monkeypatch) -> None:
        """两条指令:一条命中 PASS、一条未命中走 LLM → 各自正确 verdict + 顺序保持。"""
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        transcript_auditor = TranscriptAuditor()
        insts = [
            _tool_inst(tool_name="Bash"),  # 命中 → PASS
            _ref_inst(script_path="design.md"),  # 未命中 → LLM applicable=false → N/A
        ]
        trace = [_tc("Bash", "echo hi")]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=insts,
            evidence=JudgeTranscript(tool_trace=trace, scene="某场景", name="mix.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert len(findings) == 2
        # 顺序与输入一致
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[1].verdict == AuditVerdict.NA
        # 全部 CONDITIONAL + transcript 字段填了
        for f in findings:
            assert f.category == AuditCategory.CONDITIONAL
            assert f.transcript == "mix.jsonl"
            # evidence 全中文(不含 ASCII 句号;粗略断言非空中文)
            assert f.evidence
        # derived_from_transcript 暂默认 False(Task 8 聚合时再标)
        for f in findings:
            assert f.derived_from_transcript is False


class TestBatching:
    """分批判定:>_MAX_LLM_IN_BATCH(10)条时分批循环处理全部,无"批量超限"兜底 N/A。"""

    @staticmethod
    def test_conditional_over_batch_size_batched_all_judged(monkeypatch) -> None:
        """32 条全未命中:分 4 批(10+10+10+2)各调一次 LLM,32 条全判,无溢出兜底。

        mock 按每批 prompt 的条数返回全 applicable=false → 全 N/A;验证 call_claude
        被调 4 次、32 条全 N/A、无"批量超限"/"无法确证"兜底。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判指令 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            calls.append(bn)
            return json.dumps([{"index": i, "applicable": False} for i in range(bn)])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        transcript_auditor = TranscriptAuditor()
        insts = [_tool_inst(tool_name=f"Tool{i}") for i in range(32)]

        findings = transcript_auditor.engine.judge_conditional(
            instructions=insts,
            evidence=JudgeTranscript(tool_trace=[], scene="大场景", name="big.jsonl"),
            call_options=JudgeCallOptions(),
        )

        assert len(findings) == 32
        # 同 test_judges 批量禁令用例:并发跑批调用序不定,只锁多重集
        assert sorted(calls, reverse=True) == [10, 10, 10, 2]
        # 全 N/A(applicable=false),无兜底 N/A / 无"批量超限"
        assert all(f.verdict == AuditVerdict.NA for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        assert all("无法确证" not in f.evidence for f in findings)


def _judge_missing_tool(transcript_auditor):
    return transcript_auditor.engine.judge_conditional(
        instructions=[_tool_inst()],
        evidence=JudgeTranscript(tool_trace=[], scene="某场景", name="t.jsonl"),
        call_options=JudgeCallOptions(),
    )
