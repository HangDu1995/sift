"""judge 的 locate_turns=False:turn_refs 恒空 + prompt 无 turn 引用规则 + tool_section 无 [T{idx}]。

Task 2 单元测试:judge 层 locate_turns 开关(默认 False)。False 时
- finding.turn_refs 即便 LLM 给了也丢(FAIL/INDETERMINATE 恒空);
- judge prompt 不含 ``## turn 引用规则`` 段、输出格式不含 ``turn_refs`` 键;
- step 的 tool_section 不带 ``[T{idx}]`` 前缀、段标题不提示 ``[T{idx}]``。

全程不调真 LLM:turn_refs 丢弃用 monkeypatch 替换 ``_llm_judge_*`` 直接喂带 turn_refs 的 mask;
prompt 清洁用 monkeypatch 替换模块级 ``_run_batches_concurrent``(它收 specs,第 4 元素是 prompt)
拿到真实 prompt 断言。
"""
from __future__ import annotations

from sift.audit import auditor as auditor_mod
from sift.audit.auditor import TranscriptAuditor
from sift.models import InstructionCheck
from sift.models.audit_models import AuditVerdict
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


def _prohibition_inst(text: str = "禁止编造测试数据") -> InstructionCheck:
    """造一条禁令类指令(与 test_judge_turn_refs_parse.py 同款真实 InstructionCheck)。"""
    return InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text=text,
        section="Constraints",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _step_inst(text: str = "Step 1: 读取需求文档") -> InstructionCheck:
    """造一条步骤类指令(与 test_judge_turn_refs_parse.py 同款)。"""
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section="Process",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _with_cause(mask: list) -> tuple[list, list[str]]:
    """_llm_judge_* mock 适配:返回 (mask, cause_mask)(全 unmentioned,这些测试不验 cause)。"""
    return mask, ["unmentioned"] * len(mask)


# --- locate_turns=False → finding.turn_refs 恒空(即便 LLM 给了也丢) ------------


def test_locate_turns_off_drops_turn_refs_prohibition(monkeypatch) -> None:
    """prohibition:locate_turns=False 时即便 _llm_judge_prohibition 返回带 turn_refs 的 FAIL,
    finding.turn_refs 也恒空(跳过 sanitize/max_turn)。"""
    aud = TranscriptAuditor()
    monkeypatch.setattr(aud, "_llm_judge_prohibition", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "在 T5 违规", [5, 9]),
        (AuditVerdict.INDETERMINATE, "T7 矛盾", [7]),
    ]))
    findings = aud._judge_prohibition(
        [_prohibition_inst(), _prohibition_inst("rule 2")],
        "text_no_markers", "t.jsonl", locate_turns=False,
    )
    assert findings[0].verdict == AuditVerdict.FAIL and findings[0].turn_refs == []
    assert findings[1].verdict == AuditVerdict.INDETERMINATE and findings[1].turn_refs == []


def test_locate_turns_off_drops_turn_refs_step(monkeypatch) -> None:
    """step:locate_turns=False 时 FAIL/INDETERMINATE 的 turn_refs 也恒空。"""
    aud = TranscriptAuditor()
    monkeypatch.setattr(aud, "_llm_judge_step", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "T8 该走没走", [8]),
        (AuditVerdict.INDETERMINATE, "T5 矛盾", [5]),
    ]))
    findings = aud._judge_step(
        [_step_inst(), _step_inst("step 2")],
        "text_no_markers", "s.jsonl", locate_turns=False,
    )
    assert findings[0].verdict == AuditVerdict.FAIL and findings[0].turn_refs == []
    assert findings[1].verdict == AuditVerdict.INDETERMINATE and findings[1].turn_refs == []


# --- locate_turns=False → judge prompt 无 turn 引用规则 -----------------------


def _capture_prompt(monkeypatch, captured: dict):
    """替换模块级 _run_batches_concurrent:不调 LLM,只把 specs(prompt 在第 4 元素)存起来。"""
    def fake(specs, *a, **k):
        captured["prompts"] = [spec[3] for spec in specs]
        return [], []  # (successes, failures) 均空 → 调用方 mask 保持 (None,"",[]) → UNRESOLVED
    monkeypatch.setattr(auditor_mod, "_run_batches_concurrent", fake)


def test_locate_turns_off_prohibition_prompt_has_no_turn_rule(monkeypatch) -> None:
    """prohibition prompt:locate_turns=False 时不含 ``## turn 引用规则`` 段、
    输出格式不含 ``turn_refs`` 键(不要求 LLM 输出 turn 引用)。"""
    captured: dict = {}
    _capture_prompt(monkeypatch, captured)
    aud = TranscriptAuditor()
    aud._judge_prohibition([_prohibition_inst()], "text", "t.jsonl", locate_turns=False)
    prompt = captured["prompts"][0]
    assert "turn 引用规则" not in prompt
    assert "turn_refs" not in prompt


def test_locate_turns_off_step_prompt_has_no_turn_rule(monkeypatch) -> None:
    """step prompt:locate_turns=False 时不含 ``## turn 引用规则`` 段、输出格式不含 ``turn_refs`` 键。"""
    captured: dict = {}
    _capture_prompt(monkeypatch, captured)
    aud = TranscriptAuditor()
    aud._judge_step([_step_inst()], "text", "s.jsonl", locate_turns=False)
    prompt = captured["prompts"][0]
    assert "turn 引用规则" not in prompt
    assert "turn_refs" not in prompt


def test_locate_turns_off_step_tool_section_has_no_turn_marker(monkeypatch) -> None:
    """step tool_section:locate_turns=False 时不给工具调用加 ``[T{idx}]`` 前缀、
    段标题不提示 ``[T{idx}]``(tool_trace 里有 turn 标注也不渲染)。"""
    from sift.models import ToolCallRecord

    captured: dict = {}
    _capture_prompt(monkeypatch, captured)
    aud = TranscriptAuditor()
    aud._judge_step(
        [_step_inst()], "text", "s.jsonl", locate_turns=False,
        tool_trace=[
            ToolCallRecord(name="Read", input_summary="read foo.py", turn=3),
            ToolCallRecord(name="Bash", input_summary="run tests", turn=None),
        ],
    )
    prompt = captured["prompts"][0]
    # tool_section 段标题不再提示 [T{idx}]
    assert "标该调用所属 turn" not in prompt
    # tool 行不带 [T{idx}] 前缀(turn=3 也不渲染成 [T3])
    assert "[T3]" not in prompt
    assert "- Read: read foo.py" in prompt
    assert "- Bash: run tests" in prompt
