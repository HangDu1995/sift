"""judge 的 parse 提取 turn_refs;后验校验丢弃超范围/负数/非整数/重复;段级缺失空;judge 挂载。

Task 4 单元测试:把 LLM judge 的 turn_refs 引用从 prompt → parse → sanitize → finding 链路打通。
全程不调真 LLM:parse / sanitize 是纯函数;judge 用 monkeypatch 替换 _llm_judge_* 直接喂 mask。
"""
from __future__ import annotations

import json

from sift.audit.auditor import TranscriptAuditor
from sift.models import InstructionCheck
from sift.models.audit_models import AuditCategory, AuditVerdict
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


def _auditor() -> TranscriptAuditor:
    return TranscriptAuditor()


def _prohibition_inst(text: str = "禁止编造测试数据") -> InstructionCheck:
    """造一条禁令类指令(与 test_auditor.py 同款,真实 InstructionCheck 必填字段)。"""
    return InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text=text,
        section="Constraints",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _step_inst(text: str = "Step 1: 读取需求文档") -> InstructionCheck:
    """造一条步骤类指令(与 test_auditor.py 同款)。"""
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section="Process",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _with_cause(mask: list) -> tuple[list, list[str]]:
    """_llm_judge_* mock 适配:返回 (mask, cause_mask)。

    _llm_judge_* 第二期起返回 (mask, cause_mask);这些测试不验 UNRESOLVED/cause,
    用全 unmentioned 填满 cause_mask(同长),仅保形状正确。
    """
    return mask, ["unmentioned"] * len(mask)


# --- _sanitize_turn_refs (pure function) --------------------------------------


def test_sanitize_drops_out_of_range_negative_dup_nonint() -> None:
    """[142, 999, -1, 142, "12", 3.5, True] with max_turn=200 → [142]。

    999>200 丢,-1<1 丢,142 重复,"12" 非整数(字符串)丢,3.5 非整数(float 但不是 int)丢,
    True 是 bool(即便 isinstance(True, int) 也按非整数丢弃——防 Python 把 True 当 1)。
    """
    assert _auditor()._sanitize_turn_refs([142, 999, -1, 142, "12", 3.5, True], 200) == [142]


def test_sanitize_sorted_dedup() -> None:
    assert _auditor()._sanitize_turn_refs([5, 2, 5, 2, 3], 10) == [2, 3, 5]


def test_sanitize_zero_max_turn_drops_all() -> None:
    """max_turn<=0 表示无合法 turn 范围(无法校验任何引用)→ 全丢(空)。"""
    assert _auditor()._sanitize_turn_refs([1, 2], 0) == []


def test_sanitize_negative_max_turn_drops_all() -> None:
    assert _auditor()._sanitize_turn_refs([1, 2], -3) == []


def test_sanitize_none_input() -> None:
    assert _auditor()._sanitize_turn_refs(None, 200) == []


def test_sanitize_non_list_input() -> None:
    """LLM 偶尔把 turn_refs 写成单个整数 / 字符串 → 非 list 全丢(空)。"""
    assert _auditor()._sanitize_turn_refs(12, 200) == []  # type: ignore[arg-type]
    assert _auditor()._sanitize_turn_refs("12", 200) == []  # type: ignore[arg-type]


def test_sanitize_boundary_turn_inclusive() -> None:
    """max_turn 本身是合法 turn(max_turn=20 时 20 应保留)。"""
    assert _auditor()._sanitize_turn_refs([20], 20) == [20]


# --- _max_turn_from_text (pure function) --------------------------------------


def test_max_turn_from_text_extracts_max() -> None:
    text = "[T1] assistant: 开始\n[T12] assistant: 中段\n[T7] assistant: 结束"
    assert TranscriptAuditor._max_turn_from_text(text) == 12


def test_max_turn_from_text_no_markers() -> None:
    """transcript_text 无 [T{idx}] 标记(旧 digest / 空文本)→ 0(无法校验任何引用)。"""
    assert TranscriptAuditor._max_turn_from_text("只有普通文本,没有 turn 标记") == 0
    assert TranscriptAuditor._max_turn_from_text("") == 0


def test_max_turn_from_text_single_turn() -> None:
    assert TranscriptAuditor._max_turn_from_text("[T3] only turn") == 3


# --- _parse_prohibition / _parse_step extract turn_refs (raw) -----------------


def test_parse_prohibition_extracts_turn_refs() -> None:
    """violated=true 的 FAIL 项,LLM 输出 turn_refs=[12] → parser 透传 raw(未 sanitize)。"""
    llm_out = json.dumps([
        {"index": 0, "violated": True, "evidence": "在 T12 编了数据", "turn_refs": [12]},
        {"index": 1, "violated": False, "evidence": "ok", "turn_refs": []},
    ])
    res = TranscriptAuditor._parse_prohibition(llm_out, 2)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[0][1] == "在 T12 编了数据"
    assert res[0][2] == [12]      # raw turn_refs 透传
    assert res[1][0] == AuditVerdict.PASS
    assert res[1][2] == []


def test_parse_prohibition_default_empty_turn_refs_when_missing() -> None:
    """LLM 没输出 turn_refs 键 → 默认空 list(parser 不臆造)。"""
    llm_out = json.dumps([{"index": 0, "violated": True, "evidence": "违规"}])
    res = TranscriptAuditor._parse_prohibition(llm_out, 1)
    assert res[0][2] == []


def test_parse_prohibition_keeps_junk_for_sanitizer() -> None:
    """parser 只管透传 raw(含垃圾),sanitizer 在 judge 里做后验(职责分离)。

    本测确认 parser 不悄悄过滤负数 / 超范围 / 非整数——交由 _sanitize_turn_refs 统一处理,
    parser 保持"忠实镜像 LLM 输出"。
    """
    llm_out = json.dumps([
        {"index": 0, "violated": True, "evidence": "违规", "turn_refs": [5, -1, 999, "x"]},
    ])
    res = TranscriptAuditor._parse_prohibition(llm_out, 1)
    assert res[0][2] == [5, -1, 999, "x"]  # raw 透传,垃圾留着


def test_parse_step_extracts_turn_refs() -> None:
    """step verdict=fail,LLM 输出 turn_refs=[8, 15] → parser 透传。"""
    llm_out = json.dumps([
        {"index": 0, "verdict": "fail", "evidence": "该步未走", "turn_refs": [8, 15]},
    ])
    res = TranscriptAuditor._parse_step(llm_out, 1)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[0][1] == "该步未走"
    assert res[0][2] == [8, 15]


def test_parse_step_indeterminate_carries_turn_refs() -> None:
    """INDETERMINATE 也带 turn_refs(判不准也指向具体矛盾发生的 turn)。"""
    llm_out = json.dumps([
        {"index": 0, "verdict": "indeterminate", "evidence": "T7 说走了 T9 又说没走",
         "turn_refs": [7, 9]},
    ])
    res = TranscriptAuditor._parse_step(llm_out, 1)
    assert res[0][0] == AuditVerdict.INDETERMINATE
    assert res[0][2] == [7, 9]


def test_parse_default_is_empty_3tuple() -> None:
    """parse 失败 / 未提及 → 默认 (None, "", [])(三元素,与 PASS/FAIL/NA 同形)。"""
    res = TranscriptAuditor._parse_prohibition("非 JSON", 1)
    assert res[0] == (None, "", [])

    res2 = TranscriptAuditor._parse_step("非 JSON", 1)
    assert res2[0] == (None, "", [])


# --- segment-level absence: empty turn_refs even on FAIL ----------------------


def test_parse_prohibition_segment_absence_empty_turn_refs() -> None:
    """FAIL 但无 turn_refs(全程未发生的段级缺席)→ raw []。

    段级缺席(如"整段未编造任何数据")不指向某个具体 turn——parser 透传 LLM 给的空,
    judge 不强塞。
    """
    llm_out = json.dumps([{"index": 0, "violated": True, "evidence": "全程未发生"}])
    res = TranscriptAuditor._parse_prohibition(llm_out, 1)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[0][2] == []        # segment-level → empty


# --- judge attaches sanitized turn_refs to FAIL/INDETERMINATE only -------------


def test_judge_prohibition_attaches_sanitized_turn_refs(monkeypatch) -> None:
    """mock _llm_judge_prohibition 返回带 turn_refs 的 mask → FAIL/INDETERMINATE finding.turn_refs 经 sanitize;
    PASS 即便 LLM 给了 turn_refs 也清空(非违规不指向 turn)。"""
    aud = _auditor()
    # transcript_text 内含 [T1] / [T20] 标记 → _max_turn_from_text = 20
    ttext = "[T1] assistant: 开始\n[T20] assistant: 结束"
    monkeypatch.setattr(aud, "_llm_judge_prohibition", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "在 T15 违规", [15, 999, -1]),    # 999>20 丢,-1<1 丢 → [15]
        (AuditVerdict.PASS, "ok", [3]),                         # PASS → 清空 []
        (AuditVerdict.INDETERMINATE, "T12 矛盾", [12]),         # INDETERMINATE → 保留 [12]
    ]))
    insts = [_prohibition_inst(text=f"rule {i}") for i in range(3)]

    findings = aud._judge_prohibition(insts, ttext, "p.jsonl", locate_turns=True)

    assert len(findings) == 3
    assert findings[0].verdict == AuditVerdict.FAIL and findings[0].turn_refs == [15]
    assert findings[1].verdict == AuditVerdict.PASS and findings[1].turn_refs == []
    assert findings[2].verdict == AuditVerdict.INDETERMINATE and findings[2].turn_refs == [12]


def test_judge_prohibition_segment_absence_empty(monkeypatch) -> None:
    """段级缺席的 FAIL:LLM 给空 turn_refs(全程未发生)→ finding.turn_refs 保持空。"""
    aud = _auditor()
    ttext = "[T1] assistant: 开始\n[T5] assistant: 结束"
    monkeypatch.setattr(aud, "_llm_judge_prohibition", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "全程未发生违规行为", []),
    ]))

    findings = aud._judge_prohibition([_prohibition_inst()], ttext, "p.jsonl", locate_turns=True)

    assert findings[0].verdict == AuditVerdict.FAIL
    assert findings[0].turn_refs == []


def test_judge_step_segment_absence_empty(monkeypatch) -> None:
    """段级缺席的 FAIL(step):整段全程未走该步 → LLM 给空 turn_refs → finding.turn_refs 空。

    与 prohibition 对称(step 也走 ``self._sanitize_turn_refs(tr_raw, max_turn)``,
    tr_raw=[] → []),覆盖 FAIL 分支在 segment-absence 时的行为。
    """
    aud = _auditor()
    ttext = "[T1] assistant: 开始\n[T5] assistant: 结束"
    monkeypatch.setattr(aud, "_llm_judge_step", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "全程未走这步", []),
    ]))

    findings = aud._judge_step([_step_inst()], ttext, "s.jsonl", locate_turns=True)

    assert findings[0].verdict == AuditVerdict.FAIL
    assert findings[0].turn_refs == []


def test_judge_prohibition_no_turn_markers_still_runs(monkeypatch) -> None:
    """transcript_text 无 [T{idx}] 标记(max_turn=0)→ sanitize 全丢,FAIL 仍判但 turn_refs=[]。"""
    aud = _auditor()
    ttext = "纯文本执行记录,无 turn 标记"
    monkeypatch.setattr(aud, "_llm_judge_prohibition", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "T5 违规", [5]),   # max_turn=0 → 5 全丢
    ]))

    findings = aud._judge_prohibition([_prohibition_inst()], ttext, "p.jsonl", locate_turns=True)

    assert findings[0].verdict == AuditVerdict.FAIL
    assert findings[0].turn_refs == []   # 无合法 turn 范围,无法校验 → 空


def test_judge_step_attaches_sanitized_turn_refs(monkeypatch) -> None:
    """step 的 FAIL/INDETERMINATE 挂 sanitized turn_refs;PASS/NA 不挂。"""
    aud = _auditor()
    ttext = "[T1] assistant: 开始\n[T10] assistant: 结束"
    monkeypatch.setattr(aud, "_llm_judge_step", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, "T8 该走没走", [8, 999, 99]),  # 999/99>10 丢 → [8]
        (AuditVerdict.PASS, "走过", [2]),                   # PASS → []
        (AuditVerdict.NA, "不涉及", []),                    # NA → []
        (AuditVerdict.INDETERMINATE, "T5 矛盾", [5]),       # INDETERMINATE → [5]
    ]))
    insts = [_step_inst(text=f"step {i}") for i in range(4)]

    findings = aud._judge_step(insts, ttext, "s.jsonl", locate_turns=True)

    assert len(findings) == 4
    assert findings[0].verdict == AuditVerdict.FAIL and findings[0].turn_refs == [8]
    assert findings[1].verdict == AuditVerdict.PASS and findings[1].turn_refs == []
    assert findings[2].verdict == AuditVerdict.NA and findings[2].turn_refs == []
    assert findings[3].verdict == AuditVerdict.INDETERMINATE and findings[3].turn_refs == [5]


def test_judge_step_unresolved_keeps_empty(monkeypatch) -> None:
    """没判成(UNRESOLVED)的 finding.turn_refs 始终空(没出判定,谈不上指向哪个 turn)。"""
    aud = _auditor()
    ttext = "[T1] assistant: 开始"
    monkeypatch.setattr(aud, "_llm_judge_step", lambda *a, **k: _with_cause([
        (None, "", []),  # 没判成
    ]))

    findings = aud._judge_step([_step_inst()], ttext, "s.jsonl", locate_turns=True)

    assert findings[0].verdict == AuditVerdict.UNRESOLVED
    assert findings[0].turn_refs == []


# --- conditional 不参与(保持 turn_refs 空)-----------------------------------


def test_conditional_finding_has_empty_turn_refs(monkeypatch) -> None:
    """条件性(场景触发判定)的 finding.turn_refs 恒为空——它判的是 segment 级缺席,
    不指向具体 turn。即便 mock _llm_judge_applicable,finding 也不带 turn_refs。"""
    aud = _auditor()
    # transcript_scene(非 transcript_text)——conditional 用 user_turns 拼的 scene,
    # 本就不带 [T{idx}] 标记;即便带,conditional 也不挂 turn_refs。
    monkeypatch.setattr(aud, "_llm_judge_applicable", lambda *a, **k: _with_cause([
        (AuditVerdict.FAIL, ""),  # 该做没做(segment-level)
    ]))

    findings = aud._judge_conditional(
        [InstructionCheck(
            source=InstructionSource.TOOL_REQUIREMENT,
            instruction_text="必须用 Bash",
            section="Tools",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.TOOL_CALL_CHECK,
            params={"tool_name": "Bash"},
        )],
        tool_trace=[],                       # 未命中 → 走 LLM
        transcript_scene="某场景",
        transcript_name="c.jsonl",
    )

    assert findings[0].verdict == AuditVerdict.FAIL
    assert findings[0].category == AuditCategory.CONDITIONAL
    assert findings[0].turn_refs == []   # conditional 永不挂 turn_refs
