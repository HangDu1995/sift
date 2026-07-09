"""tests for skill_eval.audit.auditor.

_judge_conditional: 条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态对账。
全程 mock ``skill_eval.audit.auditor.call_claude``——不真跑 LLM。

覆盖矩阵:
- 命中 tool_trace → PASS 且**不调** call_claude
- 未命中 + LLM applicable=true  → FAIL
- 未命中 + LLM applicable=false → N/A
- 未命中 + LLM 抛 RuntimeError   → 保守 FAIL(低置信度)
- 未命中 + LLM 解析失败          → 保守 FAIL
- 混合(命中 PASS + 未命中走 LLM)→ 各自正确 verdict
- 批量超 30 → 溢出条保守 FAIL("批量超限未判")
- evidence 中文 / category 全 CONDITIONAL / transcript 字段填了 transcript_name
"""
from __future__ import annotations

import pytest

from skill_eval.audit.auditor import TranscriptAuditor
from skill_eval.models import InstructionCheck
from skill_eval.models.audit_models import AuditCategory, AuditVerdict
from skill_eval.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


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

    def test_runtime_error_conservative_fail(self, monkeypatch) -> None:
        """call_claude 抛 RuntimeError → 保守 FAIL(低置信度,不误判 N/A)。"""
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.CONDITIONAL
        assert f.confidence <= 0.5  # 低置信度(保守,非确证)
        assert "保守" in f.evidence

    def test_parse_failure_conservative_fail(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 解析失败 → 保守 FAIL。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "我觉得都不该触发"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert "保守" in f.evidence
        assert f.confidence <= 0.5

    def test_missing_applicable_key_is_conservative_fail(self, monkeypatch) -> None:
        """条目有 index 但缺 applicable 键 → 不得当 N/A,保守 FAIL。

        回归 I1:旧实现 ``bool(item.get("applicable"))`` = bool(None) = False
        → 被错当成 N/A(场景不涉及),漏报疑似违规。修复后应保持 None → 保守 FAIL。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL  # 保守 FAIL,不是 N/A
        assert f.confidence <= 0.5  # 低置信度(保守,非确证)
        assert "保守" in f.evidence

    def test_string_applicable_value_is_conservative_fail(self, monkeypatch) -> None:
        """applicable 是字符串 "false"(非布尔)→ 保守 FAIL,不被 bool("false") 误判。

        回归 M1:旧实现 ``bool("false")`` = True(非空串恒真)→ 错当 FAIL(方向碰巧
        对,但原理是错的);更危险的是 "true"/"false" 都会变 True。畸形非布尔值一律
        保持 None → 保守 FAIL。
        """
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": "false"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_conditional([_tool_inst()], [], "某场景", "t.jsonl")

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL  # 保守 FAIL,不是 N/A
        assert f.confidence <= 0.5  # 低置信度
        assert "保守" in f.evidence


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


class TestBatchOverflow:
    def test_over_30_overflow_conservative_fail(self, monkeypatch) -> None:
        """32 条全未命中:前 30 走 LLM,后 2 → 保守 FAIL "批量超限未判"。"""
        # LLM 只回 index 0;其余 1-29 未被提及 → None → 保守 FAIL(LLM 判定失败)
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": true}]',
        )
        auditor = TranscriptAuditor()
        insts = [_tool_inst(tool_name=f"Tool{i}") for i in range(32)]

        findings = auditor._judge_conditional(insts, [], "大场景", "big.jsonl")

        assert len(findings) == 32
        overflow = [f for f in findings if "批量超限" in f.evidence]
        assert len(overflow) == 2
        assert all(f.verdict == AuditVerdict.FAIL for f in overflow)
        assert all(f.confidence <= 0.5 for f in overflow)
        assert all(f.transcript == "big.jsonl" for f in overflow)


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

    def test_runtime_error_conservative_fail(self, monkeypatch) -> None:
        """call_claude 抛 RuntimeError → 保守 FAIL(低置信度,evidence 含'保守')。"""
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL  # 保守 FAIL
        assert f.category == AuditCategory.PROHIBITION
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

    def test_parse_failure_conservative_fail(self, monkeypatch) -> None:
        """LLM 返回非 JSON → 保守 FAIL。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude", lambda *a, **k: "看起来没违规"
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

    def test_missing_violated_key_is_conservative_fail(self, monkeypatch) -> None:
        """条目有 index 但缺 violated 键 → 严格判断,保守 FAIL(不被误当 PASS)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "不确定"}]',
        )
        auditor = TranscriptAuditor()

        findings = auditor._judge_prohibition(
            [_prohibition_inst()], "某执行记录", "p.jsonl"
        )

        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL  # 保守 FAIL,不是 PASS
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

    def test_nonbool_violated_is_conservative_fail(self, monkeypatch) -> None:
        """violated 是字符串 "true"(非布尔)→ 严格 is True/is False,保守 FAIL。

        回归:bool("true")=True 会把畸形值洗成 FAIL(方向碰巧对但原理错);
        "false" 会被 bool() 洗成 True(非空串恒真)→ 错判违规。严格判断避免这类洗 N/A / 洗 PASS。
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
        assert f.verdict == AuditVerdict.FAIL  # 保守 FAIL
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

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

    def test_unknown_verdict_conservative_fail(self, monkeypatch) -> None:
        """未知 verdict(如 'maybe')→ 保守 FAIL(不误判 N/A)。"""
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "maybe"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.category == AuditCategory.STEP
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

    def test_runtime_error_conservative_fail(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", boom)
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.confidence <= 0.5
        assert "保守" in f.evidence

    def test_missing_verdict_key_conservative_fail(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "skill_eval.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": "?"}]',
        )
        auditor = TranscriptAuditor()

        f = auditor._judge_step([_step_inst()], "某执行记录", "s.jsonl")[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.confidence <= 0.5


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
