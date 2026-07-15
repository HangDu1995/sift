"""tests for skill_eval.audit.auditor.

_judge_conditional: 条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态对账。
全程 mock ``skill_eval.audit.auditor.call_claude``——不真跑 LLM。

覆盖矩阵:
- 命中 tool_trace → PASS 且**不调** call_claude
- 未命中 + LLM applicable=true  → FAIL
- 未命中 + LLM applicable=false → N/A
- 未命中 + LLM 抛 RuntimeError(重试耗尽)→ N/A(低置信度,宁少报不乱报)
- 未命中 + LLM 解析失败          → N/A
- 混合(命中 PASS + 未命中走 LLM)→ 各自正确 verdict
- 批量超 _MAX_LLM_IN_BATCH(10)→ **分批循环处理**(每批 10 条,处理全部,无"批量超限"兜底)
- 空 prohibition/step 桶 → 不调 call_claude(短路返回 [])
- evidence 中文 / category 全 CONDITIONAL / transcript 字段填了 transcript_name
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from skill_eval.audit.auditor import _MAX_LLM_IN_BATCH, _LLM_RETRY_MAX, TranscriptAuditor
from skill_eval.models import InstructionCheck
from skill_eval.models.audit_models import AuditCategory, AuditReport, AuditVerdict
from skill_eval.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(monkeypatch) -> None:
    """重试退避的 time.sleep → no-op,保持测试快(retry 逻辑由专门测试覆盖)。"""
    monkeypatch.setattr("skill_eval.audit.auditor.time.sleep", lambda *a, **k: None)


def _tool_inst(text: str = "必须使用 Bash 工具", tool_name: str = "Bash") -> InstructionCheck:
    """造一条 TOOL_REQUIREMENT(params 含 tool_name)。"""
    return InstructionCheck(
        source=InstructionSource.TOOL_REQUIREMENT,
        instruction_text=text,
        section="Tools",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"tool_name": tool_name},
    )


def _ref_inst(text: str = "读取 design.md", script_path: str = "design.md") -> InstructionCheck:
    """造一条 REFERENCE_USAGE(params 含 script_path)。"""
    return InstructionCheck(
        source=InstructionSource.REFERENCE_USAGE,
        instruction_text=text,
        section="References",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"script_path": script_path},
    )


def _tc(name: str, input_summary: str = "") -> dict:
    """造一个 dict 风格的 tool_trace 元素(match_tool_call 支持 attr / dict)。"""
    return {"name": name, "input_summary": input_summary}


def _prohibition_inst(
    text: str = "禁止编造测试数据",
    source: InstructionSource = InstructionSource.CONSTRAINT,
) -> InstructionCheck:
    """造一条禁令类指令(CONSTRAINT / ANTI_FABRICATION)。"""
    return InstructionCheck(
        source=source,
        instruction_text=text,
        section="Constraints",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _step_inst(text: str = "Step 1: 读取需求文档") -> InstructionCheck:
    """造一条步骤类指令(HEADING_STEP)。"""
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section="Process",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _output_inst(text: str = "输出代码块", sample: str = "```python\nprint('hi')\n```") -> InstructionCheck:
    """造一条 OUTPUT_FORMAT 指令(params 含 sample,check_method=PATTERN_MATCH)。"""
    return InstructionCheck(
        source=InstructionSource.OUTPUT_FORMAT,
        instruction_text=text,
        section="Output",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.PATTERN_MATCH,
        params={"sample": sample},
    )


class TestMatchedNoLlm:
    def test_tool_match_pass_and_no_call(self, monkeypatch) -> None:
        """命中 tool_name → PASS,evidence 中文,confidence=1.0,call_claude 未被调用。"""
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        trace = [_tc("Bash", "echo hi")]

        findings = auditor._judge_conditional(
            [_tool_inst(tool_name="Bash")], trace, "某场景", "t1.jsonl"
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.CONDITIONAL
        assert f.confidence == 1.0
        assert f.transcript == "t1.jsonl"
        assert f.evidence == "trace 中调用了 Bash"
        assert f.source == "TOOL_REQUIREMENT"
        assert calls["n"] == 0  # 命中即 PASS,绝不调 LLM

    def test_script_path_match_pass(self, monkeypatch) -> None:
        """命中 script_path → PASS,evidence "trace 中读取了 {path}"。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: pytest.fail("不该调 LLM")
        )
        auditor = TranscriptAuditor()
        trace = [_tc("Read", "cat design.md")]

        findings = auditor._judge_conditional(
            [_ref_inst(script_path="design.md")], trace, "某场景", "t2.jsonl"
        )

        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence == "trace 中读取了 design.md"
        assert findings[0].source == "REFERENCE_USAGE"


class TestUnmatchedLlm:
    def test_applicable_true_is_fail(self, monkeypatch) -> None:
        """未命中 + LLM applicable=true → FAIL(该做没做),confidence≈0.8。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": true}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.CONDITIONAL
        assert f.transcript == "t.jsonl"
        assert abs(f.confidence - 0.8) < 1e-9
        assert f.evidence.startswith("场景应触发")
        assert "Bash" in f.evidence

    def test_applicable_false_is_na(self, monkeypatch) -> None:
        """未命中 + LLM applicable=false → N/A(场景不涉及),confidence≈0.8。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_ref_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.NA
        assert f.category == AuditCategory.CONDITIONAL
        assert abs(f.confidence - 0.8) < 1e-9
        assert "场景不涉及" in f.evidence

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        """call_claude 持续抛 RuntimeError(重试耗尽)→ 标 N/A(宁少报不乱报)。"""
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 不报 FAIL,标 UNCLEAR(判不准)
        assert f.category == AuditCategory.CONDITIONAL
        assert f.confidence <= 0.5  # 低置信度(判不准兜底,非 LLM 明确判 N/A)
        assert "无法确证" in f.evidence
        # 重试耗尽:1 次首发 + _LLM_RETRY_MAX 次重试,全失败才兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX

    def test_parse_failure_unverifiable_na(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 标 N/A(宁少报不乱报)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "我觉得都不该触发"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR
        assert "无法确证" in f.evidence
        assert f.confidence <= 0.5

    def test_missing_applicable_key_is_unverifiable_na(self, monkeypatch) -> None:
        """条目有 index 但缺 applicable 键 → 标 N/A(宁少报不乱报)。

        与 I1 当初相反:I1 担心"缺键被洗成 N/A → 漏报违规"故保守 FAIL;但用户原则
        '宁可少报不能乱报'推翻之——LLM 没给明确 applicable = 判不准 = N/A,绝不报假 FAIL。
        严格 ``is True/is False`` 判断保留(畸形值不洗成明确 N/A,仍走判不准兜底 N/A)。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR,不报 FAIL
        assert f.confidence <= 0.5  # 低置信度(判不准兜底)
        assert "无法确证" in f.evidence

    def test_string_applicable_value_is_unverifiable_na(self, monkeypatch) -> None:
        """applicable 是字符串 "false"(非布尔)→ 标 N/A(宁少报不乱报)。

        回归 M1:旧实现 ``bool("false")`` = True(非空串恒真)会洗畸形值。现保留严格
        ``is True/is False`` 判断(畸形非布尔值不当明确结论),落到判不准兜底 → N/A。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": "false"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR,不报 FAIL
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence


class TestRetryRecovery:
    """整批 transient 失败时重试救回:前几次 RuntimeError、后续成功 → 正常判定,不兜底 N/A。

    回归 audit-out3:2 个整批(25 条)撞限流直接放弃 → N/A。重试后 transient 失败大多
    能救回,真判(给出 resolved verdict)而非兜底。
    """

    def test_retry_recovers_then_judges_normally(self, monkeypatch) -> None:
        """call_claude 前 _LLM_RETRY_MAX 次 RuntimeError、第 3 次成功 → 重试救回,resolved N/A。"""
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] <= _LLM_RETRY_MAX:  # 前 _LLM_RETRY_MAX 次 transient 失败
                raise RuntimeError("transient rate limit")
            return '[{"index": 0, "applicable": false}]'  # 第 3 次:N/A resolved

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", flaky)
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.NA  # applicable=false → N/A
        # 关键:resolved(0.8)而非兜底(0.3)→ 重试成功救回,真判了
        assert f.confidence >= 0.7
        assert "场景不涉及" in f.evidence  # LLM 给的 evidence,非"无法确证"兜底
        assert calls["n"] == 1 + _LLM_RETRY_MAX  # 重试 _LLM_RETRY_MAX 次后第 3 次成功


class TestMixed:
    def test_match_and_unmixed_verdicts(self, monkeypatch) -> None:
        """两条指令:一条命中 PASS、一条未命中走 LLM → 各自正确 verdict + 顺序保持。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        auditor = TranscriptAuditor()
        insts = [
            _tool_inst(tool_name="Bash"),       # 命中 → PASS
            _ref_inst(script_path="design.md"),  # 未命中 → LLM applicable=false → N/A
        ]
        trace = [_tc("Bash", "echo hi")]

        findings = auditor._judge_conditional(insts, trace, "某场景", "mix.jsonl")

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

    def test_conditional_over_batch_size_batched_all_judged(self, monkeypatch) -> None:
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

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_tool_inst(tool_name=f"Tool{i}") for i in range(32)]

        findings = auditor._judge_conditional(insts, [], "大场景", "big.jsonl")

        assert len(findings) == 32
        assert calls == [10, 10, 10, 2]  # 分 10+10+10+2 四批
        # 全 N/A(applicable=false),无兜底 N/A / 无"批量超限"
        assert all(f.verdict == AuditVerdict.NA for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        assert all("无法确证" not in f.evidence for f in findings)


# ---------------------------------------------------------------------------
# classify: 指令类别路由
# ---------------------------------------------------------------------------


class TestClassify:
    """classify 把 InstructionSource 路由到对应的 AuditCategory。"""

    @pytest.mark.parametrize(
        "source,expected",
        [
            (InstructionSource.TOOL_REQUIREMENT, AuditCategory.CONDITIONAL),
            (InstructionSource.REFERENCE_USAGE, AuditCategory.CONDITIONAL),
            (InstructionSource.CONSTRAINT, AuditCategory.PROHIBITION),
            (InstructionSource.ANTI_FABRICATION, AuditCategory.PROHIBITION),
            (InstructionSource.HEADING_STEP, AuditCategory.STEP),
            (InstructionSource.OUTPUT_FORMAT, AuditCategory.OUTPUT),
        ],
    )
    def test_routing(self, source: InstructionSource, expected: AuditCategory) -> None:
        inst = InstructionCheck(
            source=source,
            instruction_text="x",
            section="s",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
            params={},
        )
        assert TranscriptAuditor.classify(inst) == expected


# ---------------------------------------------------------------------------
# _judge_prohibition: 禁令类(CONSTRAINT / ANTI_FABRICATION)主动检测违规
# ---------------------------------------------------------------------------


class TestJudgeProhibition:
    def test_violated_true_is_fail(self, monkeypatch) -> None:
        """violated=true → FAIL(违规了),evidence 取 LLM 返回,confidence≈0.8。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": true, "evidence": "执行中编造了测试数据"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.PROHIBITION
        assert f.transcript == "p.jsonl"
        assert f.evidence == "执行中编造了测试数据"
        assert abs(f.confidence - 0.8) < 1e-9
        assert f.source == "CONSTRAINT"

    def test_violated_false_is_pass(self, monkeypatch) -> None:
        """violated=false → PASS(没违规)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "未发现编造行为"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.PROHIBITION
        assert f.evidence == "未发现编造行为"
        assert abs(f.confidence - 0.8) < 1e-9

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        """call_claude 抛 RuntimeError → 标 N/A(宁少报不乱报:禁令也不在无确证时报 FAIL)。"""
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR,不报假 FAIL
        assert f.category == AuditCategory.PROHIBITION
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_parse_failure_unverifiable_na(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 标 N/A(宁少报不乱报)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "看起来没违规"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_missing_violated_key_is_unverifiable_na(self, monkeypatch) -> None:
        """条目有 index 但缺 violated 键 → 严格判断,标 N/A(不被误当 PASS,也不报假 FAIL)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "不确定"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR,既不当 PASS 也不报 FAIL
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_nonbool_violated_is_unverifiable_na(self, monkeypatch) -> None:
        """violated 是字符串 "true"(非布尔)→ 严格 is True/is False,标 N/A。

        回归:bool("true")=True 会把畸形值洗成 FAIL(方向碰巧对但原理错);
        "false" 会被 bool() 洗成 True(非空串恒真)→ 错判违规。严格判断避免洗 PASS;
        畸形值落判不准兜底 → N/A(宁少报不乱报,不报假 FAIL)。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": "false"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_anti_fabrication_source(self, monkeypatch) -> None:
        """ANTI_FABRICATION 也走 prohibition 判定。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "无编造"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst(source=InstructionSource.ANTI_FABRICATION)],
            "某执行记录",
            "p.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.source == "ANTI_FABRICATION"
        assert f.category == AuditCategory.PROHIBITION

    def test_empty_instructions_no_llm(self, monkeypatch) -> None:
        """空 prohibition 桶(0 条)→ 不调 call_claude,直接返回 [](短路)。

        回归 Issue 1:空桶仍 spawn claude -p(生成"待判 0 条"prompt)浪费调用,
        与 audit"零重跑低成本"相悖。短路后不再调 LLM。
        """
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition([], "某执行记录", "empty.jsonl")

        assert findings == []
        assert calls["n"] == 0  # 空桶短路,未 spawn LLM

    def test_over_batch_size_batched_all_judged(self, monkeypatch) -> None:
        """35 条禁令:分 4 批(10+10+10+5)处理,全部判 PASS,无"批量超限"兜底。

        回归 Issue 2:旧实现单次截断、超限兜底("批量超限未判"),假 FAIL 淹没真问题。
        分批后 35 条全判(mock 按批返回 violated=false → 全 PASS)。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判禁令 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            calls.append(bn)
            return json.dumps([
                {"index": i, "violated": False, "evidence": "未违反"}
                for i in range(bn)
            ])

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_prohibition_inst(text=f"禁令 #{i}") for i in range(35)]

        findings = auditor._judge_prohibition(insts, "某执行记录", "batch.jsonl")

        assert len(findings) == 35
        assert calls == [10, 10, 10, 5]  # 分 10+10+10+5 四批
        assert all(f.verdict == AuditVerdict.PASS for f in findings)
        assert all(f.category == AuditCategory.PROHIBITION for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        assert all("保守" not in f.evidence for f in findings)


# ---------------------------------------------------------------------------
# _judge_step: 步骤类(HEADING_STEP)三态判定
# ---------------------------------------------------------------------------


class TestJudgeStep:
    def test_verdict_pass(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "pass", "evidence": "走过该步"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.STEP
        assert f.transcript == "s.jsonl"
        assert f.evidence == "走过该步"
        assert abs(f.confidence - 0.8) < 1e-9

    def test_verdict_fail(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "该走没走"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        assert findings[0].verdict == AuditVerdict.FAIL
        assert findings[0].category == AuditCategory.STEP

    def test_verdict_na(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "na", "evidence": "场景不涉及"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")

        assert findings[0].verdict == AuditVerdict.NA
        assert findings[0].category == AuditCategory.STEP

    def test_verdict_case_insensitive(self, monkeypatch) -> None:
        """verdict 大小写不敏感:PASS / Fail / NA 都能识别。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "PASS"}]',
        )
        auditor = TranscriptAuditor()
        assert auditor._judge_step([_step_inst()], "x", "s.jsonl")[0].verdict == AuditVerdict.PASS

    def test_tool_trace_included_in_prompt(self, monkeypatch) -> None:
        """涉及工具的步骤,tool_trace 应进 prompt 让 LLM 看到实际工具调用。

        canary(log-error-analyzer)暴露的缺口:Read input log 步骤明明调了 Read,
        但 transcript_text 只剩 assistant 文字声称 → LLM 看不到实际调用 → 假 FAIL。
        修复:tool_trace 进 step 判定 prompt。
        """
        from skill_eval.models import ToolCallRecord

        captured: dict = {}

        def fake(prompt, *a, **k):
            captured["prompt"] = prompt
            return '[{"index": 0, "verdict": "pass", "evidence": "走过"}]'

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        trace = [ToolCallRecord(name="Read", input_summary='{"file_path": "/x/logs/app.log"}')]

        auditor._judge_step([_step_inst()], "执行记录", "s.jsonl", tool_trace=trace)

        assert "app.log" in captured["prompt"], "tool_trace 应进 step 判定 prompt"
        assert "Read" in captured["prompt"]

    def test_no_tool_trace_omits_section(self, monkeypatch) -> None:
        """不传 tool_trace 时 prompt 不含工具调用段(向后兼容,现有调用不破)。"""
        captured: dict = {}

        def fake(prompt, *a, **k):
            captured["prompt"] = prompt
            return '[{"index": 0, "verdict": "pass", "evidence": "走过"}]'

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        auditor._judge_step([_step_inst()], "执行记录", "s.jsonl")
        assert "工具调用记录" not in captured["prompt"]

    def test_unknown_verdict_unverifiable_na(self, monkeypatch) -> None:
        """未知 verdict(如 'maybe')→ 标 N/A(宁少报不乱报:判不准不报 FAIL)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "maybe"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR,不报 FAIL
        assert f.category == AuditCategory.STEP
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_runtime_error_unverifiable_na(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_missing_verdict_key_unverifiable_na(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "?"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNCLEAR  # 判不准 → UNCLEAR
        assert f.confidence <= 0.5
        assert "无法确证" in f.evidence

    def test_empty_instructions_no_llm(self, monkeypatch) -> None:
        """空 step 桶(0 条)→ 不调 call_claude,直接返回 [](短路)。

        回归 Issue 1:空桶仍 spawn claude -p 浪费调用。短路后不再调 LLM。
        """
        calls = {"n": 0}

        def fake(*a, **k):
            calls["n"] += 1
            return "[]"

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()

        findings = auditor._judge_step([], "某执行记录", "empty.jsonl")

        assert findings == []
        assert calls["n"] == 0  # 空桶短路,未 spawn LLM

    def test_over_60_batched_index_mapping(self, monkeypatch) -> None:
        """62 条步骤:分 7 批(10×6+2)处理,7 次 call_claude,全判且 index 不错位。

        回归 Issue 2 + 验证分批易错点(index 映射):mock 按调用次序把全局位置写进
        evidence(批 k 的本地 i → 全局 k*10+i),据此验证批间 index 映射——批 2 本地
        [0] 正确落到全局 [10]、批 7 本地 [0]→[60],而非覆盖 [0]。错位会把某批结果
        写回错位置或漏写,被 evidence 的 global# 与原位置不一致抓出。
        """
        calls: list[int] = []

        def fake(prompt, *a, **k):
            m = re.search(r"待判步骤 \((\d+) 条\)", prompt)
            bn = int(m.group(1)) if m else 0
            call_idx = len(calls)
            start = call_idx * _MAX_LLM_IN_BATCH
            calls.append(bn)
            return json.dumps([
                {"index": i, "verdict": "pass", "evidence": f"global#{start + i}"}
                for i in range(bn)
            ])

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        auditor = TranscriptAuditor()
        insts = [_step_inst(text=f"步骤 #{i}") for i in range(62)]

        findings = auditor._judge_step(insts, "某执行记录", "big.jsonl")

        assert len(findings) == 62
        assert calls == [10, 10, 10, 10, 10, 10, 2]  # 7 批(10×6+2)
        assert all(f.verdict == AuditVerdict.PASS for f in findings)
        assert all("批量超限" not in f.evidence for f in findings)
        # index 映射:evidence 的 global# 与原位置严格一致
        # (批 2 本地[0]→全局[10]、批 7 本地[0]→[60],不错位、不覆盖)
        for i, f in enumerate(findings):
            assert f.evidence == f"global#{i}", f"位置 {i} 映射错位: {f.evidence}"


# ---------------------------------------------------------------------------
# _judge_output: OUTPUT_FORMAT(PATTERN_MATCH)确定性子串判定,不调 LLM
# ---------------------------------------------------------------------------


class TestJudgeOutput:
    def test_no_llm_called(self, monkeypatch) -> None:
        """_judge_output 是确定性判定,绝不调 call_claude。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("_judge_output 不该调 LLM"),
        )
        auditor = TranscriptAuditor()
        auditor._judge_output([_output_inst()], "任意文本", "o.jsonl")

    def test_sample_substring_match_is_pass(self) -> None:
        """sample 某行(strip 后)作为子串出现在 transcript_text → PASS,confidence=1.0。"""
        auditor = TranscriptAuditor()
        # sample 含 "print('hi')" 这一行;transcript 里出现该子串 → PASS
        findings = auditor._judge_output(
            [_output_inst(sample="```python\nprint('hi')\n```")],
            "结果是 print('hi') 的输出",
            "o.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.PASS
        assert f.category == AuditCategory.OUTPUT
        assert f.confidence == 1.0
        assert f.transcript == "o.jsonl"
        assert f.source == "OUTPUT_FORMAT"
        assert "期望格式" in f.evidence

    def test_sample_no_match_is_fail(self) -> None:
        """sample 行都不在 transcript_text → FAIL,confidence=1.0。"""
        auditor = TranscriptAuditor()
        findings = auditor._judge_output(
            [_output_inst(sample="```python\nprint('hi')\n```")],
            "完全无关的文本",
            "o.jsonl",
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.OUTPUT
        assert f.confidence == 1.0
        assert "未找到期望格式" in f.evidence

    def test_multiline_any_line_matches(self) -> None:
        """多行 sample:任一行(strip 后)命中 → PASS(照 checker PATTERN_MATCH 逻辑)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="line one\nSPECIAL_TOKEN\nline three")
        findings = auditor._judge_output([inst], "中间有 SPECIAL_TOKEN 出现", "o.jsonl")

        assert findings[0].verdict == AuditVerdict.PASS

    def test_strips_lines_before_match(self) -> None:
        """sample 行带前后空白 → strip 后再作子串匹配(照 checker 逻辑)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="  padded_token  ")
        findings = auditor._judge_output([inst], "hit padded_token here", "o.jsonl")

        assert findings[0].verdict == AuditVerdict.PASS

    def test_empty_sample_is_fail(self) -> None:
        """sample 为空 → 无可匹配行 → FAIL(照 checker PATTERN_MATCH 空样本行为)。"""
        auditor = TranscriptAuditor()
        inst = _output_inst(sample="")
        findings = auditor._judge_output([inst], "任意文本", "o.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.OUTPUT
        assert f.confidence == 1.0

    def test_multiple_instructions_order_preserved(self) -> None:
        """多条 OUTPUT_FORMAT 指令:顺序与输入一致,各自判定。"""
        auditor = TranscriptAuditor()
        insts = [
            _output_inst(text="格式A", sample="AAA_TOKEN"),
            _output_inst(text="格式B", sample="BBB_TOKEN"),
        ]
        findings = auditor._judge_output([insts[0], insts[1]], "只有 AAA_TOKEN", "o.jsonl")

        assert len(findings) == 2
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[1].verdict == AuditVerdict.FAIL
        for f in findings:
            assert f.category == AuditCategory.OUTPUT
            assert f.transcript == "o.jsonl"


# ---------------------------------------------------------------------------
# audit: 聚合主入口(串联 InstructionExtractor + rebuild + derive + 四 _judge_*)
# 全程 mock skill_eval.audit.auditor.call_claude —— 不真跑 LLM。
# mock 设计:fake 返回一个覆盖 0..59 的 JSON 数组,每条同时给 applicable / violated /
# verdict + evidence 三个键。三个 LLM 判定方法各只读自己关心的键(_parse_applicable 读
# applicable、_parse_prohibition 读 violated、_parse_step 读 verdict),互不干扰 ——
# 一个通用结构兼容三种判定格式,无需按 prompt 内容分支。
# ---------------------------------------------------------------------------


def _pass_fake():
    """通用 mock call_claude:返回全"通过"语义的 JSON 数组(覆盖 0..59)。

    - 条件性未命中 → applicable=false → N/A(场景不涉及)
    - 禁令 → violated=false → PASS(没违规)
    - 步骤 → verdict="pass" → PASS(走过了)

    一个结构喂三个判定方法,便于聚合测试数 finding / 算 summary。
    """
    def fake(prompt, *a, **k):
        return json.dumps([
            {
                "index": i,
                "applicable": False,
                "violated": False,
                "verdict": "pass",
                "evidence": "通过",
            }
            for i in range(60)
        ])

    return fake


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """把 records 写成 JSONL(每行一个 JSON 对象),返回该路径。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _read_file_records(
    file_path: str,
    content: str = "文件内容",
    user_text: str = "请分析这个文件",
) -> list[dict]:
    """造一组 session JSONL 记录:user turn + Read(file_path) + 其 tool_result。

    让 build_digest 产出非空 digest:user_turns 非空、tool_trace 含 Read、
    input_files 含 file_path(未被改写 → 计入输入),供反推/对账用。
    """
    return [
        {"message": {"role": "user", "content": user_text}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": file_path},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": content}
                ],
            }
        },
    ]


class TestAuditAggregate:
    """audit 主入口:多 transcript 聚合、summary 计数、derived 标记、坏 transcript 降级。"""

    def test_two_transcripts_aggregate_with_summary_and_derived(
        self, tmp_path, monkeypatch
    ) -> None:
        """2 transcript 各 Read foo.md:每条产 2 finding(1 N/A + 1 PASS-derived),平铺 4 条。

        - skill_body "use Bash" → 1 条 TOOL_REQUIREMENT(未命中 trace → LLM N/A);
        - foo.md 不在 skill_body → derive_declarations 反推 "读取 foo.md"(REFERENCE_USAGE),
          且 transcript 真读了 → 命中 trace → PASS,标 derived_from_transcript=True。
        每 transcript 2 finding → 共 4;summary total/pass/fail/na 精确。
        """
        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        tp1 = _write_jsonl(tmp_path / "t1.jsonl", _read_file_records(str(foo)))
        tp2 = _write_jsonl(tmp_path / "t2.jsonl", _read_file_records(str(foo)))

        auditor = TranscriptAuditor()
        report = auditor.audit("myskill", "use Bash", [tp1, tp2])

        assert isinstance(report, AuditReport)
        assert report.skill_name == "myskill"
        # transcripts 字段含 2 个文件名(顺序与入参一致)
        assert report.transcripts == ["t1.jsonl", "t2.jsonl"]
        assert report.warnings == []

        # 平铺 per-transcript:每 transcript 2 finding → 共 4
        assert len(report.findings) == 4
        # 每 transcript 的 finding 各自带正确的 transcript 名(2 + 2)
        assert sorted(f.transcript for f in report.findings) == ["t1.jsonl", "t1.jsonl", "t2.jsonl", "t2.jsonl"]

        # summary 精确:每 transcript (1 N/A [Bash 未调] + 1 PASS [读取 foo.md 命中])
        assert report.summary == {"total": 4, "pass": 2, "fail": 0, "na": 2, "unclear": 0}

        # derived 标记:2 个 PASS finding(读取 foo.md)标 derived_from_transcript=True,
        # 2 个 N/A finding(use Bash,来自 skill_body 静态指令)保持 False
        derived = [f for f in report.findings if f.derived_from_transcript]
        assert len(derived) == 2
        assert all(f.verdict == AuditVerdict.PASS for f in derived)
        assert all(f.category == AuditCategory.CONDITIONAL for f in derived)
        assert all("foo.md" in f.instruction_text for f in derived)
        non_derived = [f for f in report.findings if not f.derived_from_transcript]
        assert len(non_derived) == 2
        assert all(f.verdict == AuditVerdict.NA for f in non_derived)

    def test_bad_transcript_goes_to_warning_others_processed(
        self, tmp_path, monkeypatch
    ) -> None:
        """坏 transcript(不存在的路径)→ rebuild 抛 SkillEvalError → 进 warnings、不崩;
        好 transcript 照常处理(findings 正常产出)。"""
        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", _pass_fake())

        foo = tmp_path / "foo.md"
        good = _write_jsonl(tmp_path / "good.jsonl", _read_file_records(str(foo)))
        bad = tmp_path / "missing.jsonl"  # 不存在

        auditor = TranscriptAuditor()
        report = auditor.audit("s", "use Bash", [good, bad])

        # 好的正常处理(进 transcripts + findings)
        assert report.transcripts == ["good.jsonl"]
        assert len(report.findings) == 2  # good 的 2 finding
        # 坏的不进 transcripts / findings
        assert all("missing" not in f.transcript for f in report.findings)
        # 坏的进 warnings(消息含文件名)
        assert len(report.warnings) == 1
        assert "missing.jsonl" in report.warnings[0]

    def test_empty_transcripts_returns_empty_report(self, monkeypatch) -> None:
        """空 transcripts 列表 → findings 空、summary 全 0、transcripts 空,不崩。"""
        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", _pass_fake())

        auditor = TranscriptAuditor()
        report = auditor.audit("s", "use Bash", [])

        assert isinstance(report, AuditReport)
        assert report.findings == []
        assert report.transcripts == []
        assert report.warnings == []
        assert report.summary == {"total": 0, "pass": 0, "fail": 0, "na": 0, "unclear": 0}

    def test_routes_each_category(self, tmp_path, monkeypatch) -> None:
        """skill_body 含四类指令 + REFERENCES → 各自路由到正确 category 判定(覆盖四桶)。

        TOOL_REQUIREMENT/REFERENCE_USAGE→CONDITIONAL、CONSTRAINT→PROHIBITION、
        HEADING_STEP→STEP、OUTPUT_FORMAT→OUTPUT。
        """
        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", _pass_fake())

        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze input format\n\n"
            "```\nEXPECTED_TOKEN\n```\n\n"
            "read references/design.md\n"
        )
        # 最小 transcript:仅 user turn(rebuild 非空即可;无工具 → 条件性全未命中走 LLM)
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )

        auditor = TranscriptAuditor()
        report = auditor.audit("s", body, [tp])

        # 四个类别都被路由到(各自走了对应的 _judge_*)
        cats = {f.category for f in report.findings}
        assert cats == {
            AuditCategory.CONDITIONAL,
            AuditCategory.PROHIBITION,
            AuditCategory.STEP,
            AuditCategory.OUTPUT,
        }
        assert len(report.findings) == 5  # 2 CONDITIONAL + 1 + 1 + 1


class TestProgressCallback:
    """on_progress 回调:audit 长跑(每批 LLM 40-257s)时让用户看到进度,而非全程静默。

    契约:audit() / _judge_conditional / _judge_prohibition / _judge_step 接可选
    on_progress(str)->None,默认 None(核心不耦合 I/O)。进 transcript 打头;每个 LLM
    批前后各打一行——类别名前置括号化([类别],三类并发交错可分清)、单批不显序号、
    多批显 "批 k/总"、+ 完成耗时。on_progress=None 时行为与旧版一致。
    """

    def test_conditional_judge_emits_per_batch_progress(self, monkeypatch) -> None:
        """未命中走 LLM → on_progress 收到 [条件性] 批 + 完成耗时行(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_conditional(
            [_tool_inst()], [], "某场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[条件性]" in flat  # 类别名前置括号化(三类并发交错可分清)
        assert "[1/1]" not in flat  # 单批不显 [k/总](原 [1/1] 是噪声)
        assert "⏱" in flat  # 完成耗时行

    def test_conditional_multi_batch_progress_indexed(self, monkeypatch) -> None:
        """>_MAX_LLM_IN_BATCH(12 条)→ 分 2 批,on_progress 标 "批 1/2" 与 "批 2/2"。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: json.dumps(
                [{"index": i, "applicable": False} for i in range(_MAX_LLM_IN_BATCH)]
            ),
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_conditional(
            [_tool_inst()] * 12, [], "场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "批 1/2" in flat
        assert "批 2/2" in flat

    def test_prohibition_judge_emits_progress(self, monkeypatch) -> None:
        """禁令批 → on_progress 含 [禁令] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": "未违反"}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_prohibition(
            [_prohibition_inst()], "执行记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[禁令]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    def test_step_judge_emits_progress(self, monkeypatch) -> None:
        """步骤批 → on_progress 含 [步骤] 标签 + ⏱(单批不显 [1/1])。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "pass", "evidence": "走过"}]',
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_step(
            [_step_inst()], "执行记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "[步骤]" in flat
        assert "[1/1]" not in flat
        assert "⏱" in flat

    def test_empty_bucket_does_not_call_progress(self, monkeypatch) -> None:
        """空禁令桶短路 → 不 spawn LLM → on_progress 不被调(不报假进度)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("空桶不应调 LLM"),
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        auditor._judge_prohibition([], "执行记录", "t.jsonl", on_progress=msgs.append)
        assert msgs == []

    def test_audit_emits_transcript_header(self, tmp_path, monkeypatch) -> None:
        """audit() 进每个 transcript 打头:'对账 transcript i/N: <name>'。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false}]',
        )
        body = (
            "use Bash\n\n"
            "NEVER delete production data\n\n"
            "1. Analyze the input format carefully\n\n"
            "```\nEXPECTED_TOKEN\n```\n"
        )
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        msgs: list[str] = []
        TranscriptAuditor().audit("s", body, [tp], on_progress=msgs.append)
        flat = " ".join(msgs)
        assert "对账 transcript 1/1" in flat
        assert "only_user.jsonl" in flat

    def test_progress_none_default_is_noop(self, monkeypatch) -> None:
        """on_progress=None(默认)→ 与旧版完全一致,无 AttributeError。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "violated": false, "evidence": ""}]',
        )
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition([_prohibition_inst()], "记录", "t.jsonl")
        assert len(findings) == 1  # 不传 on_progress 也能正常判定

    def test_failure_progress_includes_reason(self, monkeypatch) -> None:
        """整批 LLM 失败时,进度行要带**原因**(str(exc)),不只'兜底 N/A'。

        回归真实痛点:旧版 ``except RuntimeError: continue`` 把异常吞了,进度只写
        '失败(整批兜底 N/A)'——用户看到'都判定失败'却不知为什么(超时?exit 1?限流?)。
        带 str(exc) 后,失败原因(如 'Reached max turns' / 'timed out after 600s')
        即时可见,才好诊断"都判定失败"的根因。
        """
        def boom(*a, **k):
            raise RuntimeError("claude CLI failed (exit 1): Reached max turns (1)")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "失败" in flat
        assert "Reached max turns" in flat  # 原因带出来了,不只'兜底 N/A'
        # 判定本身仍兜底为 UNCLEAR(判不准,失败不报假 FAIL)
        assert findings[0].verdict == AuditVerdict.UNCLEAR

    def test_zero_parsed_batch_emits_warning_prohibition(self, monkeypatch) -> None:
        """调用成功但整批 0 条解析成功(LLM 输出畸形/空)→ 打可见警告,不只静默 N/A。

        回归盲点:批"完成 ⏱ Xs"看着像成功,实则 LLM 返回畸形/截断/空数组 → _parse_*
        全 None → 全兜底 N/A,却**毫无信号**(失败原因日志只覆盖 RuntimeError 调用失败,
        不覆盖这种"调用成功但解析为空")。补一条 ⚠ 警告,让静默 N/A 可见。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "[]",  # 空数组 → 0 条解析
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst(), _prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "完成" in flat  # 调用确实成功了(不是 RuntimeError)
        assert "0 条解析成功" in flat  # ⚠ 但解析为空,可见警告
        assert all(f.verdict == AuditVerdict.UNCLEAR for f in findings)  # 仍兜底 UNCLEAR(判不准)

    def test_zero_parsed_batch_emits_warning_conditional(self, monkeypatch) -> None:
        """条件性批(标量 mask 形状)同样:0 条解析成功 → ⚠ 警告。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "[]",  # 空数组 → mask 全 None
        )
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_conditional(
            [_tool_inst(), _tool_inst()], [], "场景", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "0 条解析成功" in flat
        assert all(f.verdict == AuditVerdict.UNCLEAR for f in findings)

    def test_zero_parsed_triggers_retry_and_recovers(self, monkeypatch) -> None:
        """0 解析成功 → 自动重试,重试拿到能解析的输出 → 真 verdict(非兜底 N/A)。

        实锤价值:同 prompt 重试常能解出(0-解析多为 LLM 输出方差的瞬态),故重试而非直接
        放弃。第一次返回空数组(0 解析)→ 重试 → 第二次返回正常 JSON → 解出 resolved PASS。
        """
        calls = {"n": 0}

        def flaky(prompt, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return "[]"  # 第一次:0 解析
            return '[{"index": 0, "violated": false, "evidence": "重试后解出"}]'  # 重试成功

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", flaky)
        msgs: list[str] = []
        auditor = TranscriptAuditor()
        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "记录", "t.jsonl", on_progress=msgs.append,
        )
        flat = " ".join(msgs)
        assert "重试" in flat  # 触发了重试
        assert calls["n"] == 2  # 第一次 0 解析 + 重试一次成功
        assert findings[0].verdict == AuditVerdict.PASS  # violated=false → PASS(resolved)
        assert findings[0].confidence >= 0.7  # resolved(0.8),非 0.3 兜底
