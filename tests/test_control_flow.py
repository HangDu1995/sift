"""tests for 控制流判定:ORDER 顺序对 + QUANTITY 数量(audit 引擎新维度)。

四层:
- 检测层(``sift.audit.control_flow``,纯函数):pair 检测口径(子序列相邻/连续同
  section 段/负向排除)、数量检测(count/distributive、检测文本用 source_excerpt、
  中文数字不支持)、``count_tool_calls`` 计数谓词;
- 判官(``_judge_control_flow``,mock call_claude):程序判先行、LLM 四态、na 口径
  (没做=fail 不洗 na)、tool_trace 段进 prompt、turn_refs 受 locate_turns 整链门控;
- synthetic 下游(dedup/noise/aggregate):link_related 跳过、噪声豁免、
  by_instruction/coverage 不进分母;
- audit() 接线:移交路由(含禁令桶)、related 回填(端点按原始 instruction_text 查,
  headline 仅显示)、端点缺失丢弃、判官整体异常兜底、无控制流信号零行为变化。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sift.audit.aggregate import aggregate_by_instruction, aggregate_coverage
from sift.audit.auditor import TranscriptAuditor
from sift.audit.control_flow import (
    OrderPair,
    QuantitySpec,
    _ORDER_RE,
    detect_order_pairs,
    detect_quantity,
    order_instruction_text,
)
from sift.audit.dedup import assign_sequence, link_related
from sift.audit.noise import annotate_noise
from sift.audit.tool_match import count_tool_calls
from sift.models import InstructionCheck
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
)
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(monkeypatch) -> None:
    """重试退避的 time.sleep → no-op(同 test_auditor 惯例)。"""
    monkeypatch.setattr("sift.audit.auditor.time.sleep", lambda *a, **k: None)


# ---- 构造 helper(仿 test_auditor.py)--------------------------------------------


def _step(text: str, section: str = "步骤", excerpt: str = "", headline: str = "") -> InstructionCheck:
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section=section,
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
        source_excerpt=excerpt,
        headline=headline,
    )


def _constraint(text: str, excerpt: str = "") -> InstructionCheck:
    return InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text=text,
        section="规则",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
        source_excerpt=excerpt,
    )


def _tool_req(text: str, tool_name: str, excerpt: str = "") -> InstructionCheck:
    return InstructionCheck(
        source=InstructionSource.TOOL_REQUIREMENT,
        instruction_text=text,
        section="工具",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"tool_name": tool_name},
        source_excerpt=excerpt,
    )


def _tc(name: str, input_summary: str = "") -> dict:
    return {"name": name, "input_summary": input_summary}


# ---------------------------------------------------------------------------
# 顺序连词词表:负向排除(优先/不再/预先)+ 英文 \b
# ---------------------------------------------------------------------------


class TestOrderRegex:
    @pytest.mark.parametrize("text", ["优先处理高严重度", "优先级从高到低", "预先创建目录", "不再检查旧文件", "这个问题不再发生"])
    def test_negative_exclusions(self, text: str) -> None:
        assert not _ORDER_RE.search(text), f"「{text}」不应命中顺序词"

    @pytest.mark.parametrize("text", ["先读取日志再分类", "首先读取输入", "然后写报告", "分类之后再汇总", "最后清理临时文件", "下一步运行测试"])
    def test_positive_hits(self, text: str) -> None:
        assert _ORDER_RE.search(text), f"「{text}」应命中顺序词"

    def test_english_word_boundary(self) -> None:
        assert not _ORDER_RE.search("authentic output")  # then 不得命中 authentic
        assert _ORDER_RE.search("first read, then write")
        assert _ORDER_RE.search("Run it Next")  # 大小写不敏感


# ---------------------------------------------------------------------------
# detect_order_pairs:子序列相邻 + 连续同 section 段
# ---------------------------------------------------------------------------


class TestDetectOrderPairs:
    def test_adjacent_same_section_with_connective(self) -> None:
        insts = [_step("先读取输入日志"), _step("然后分类错误"), _step("写出报告")]
        pairs = detect_order_pairs(insts)
        # 相邻对 (A,B) 命中(A 含「先」);(B,C) 也相邻且 B 含「然后」→ 同样命中
        assert [(p.a.instruction_text, p.b.instruction_text) for p in pairs] == [
            ("先读取输入日志", "然后分类错误"),
            ("然后分类错误", "写出报告"),
        ]

    def test_connective_in_either_side(self) -> None:
        insts = [_step("读取输入日志"), _step("分类错误,然后再写报告")]
        assert len(detect_order_pairs(insts)) == 1  # 连词在 B 侧也算

    def test_no_connective_no_pair(self) -> None:
        insts = [_step("读取输入日志"), _step("分类错误"), _step("写报告")]
        assert detect_order_pairs(insts) == []

    def test_cross_section_not_paired(self) -> None:
        insts = [_step("先读取输入日志", section="准备"), _step("然后分类错误", section="执行")]
        assert detect_order_pairs(insts) == []

    def test_same_named_sections_do_not_cross_pair(self) -> None:
        # 同名字标题出现两次:两段各自配对,不跨段(按连续同 section 段分组)
        insts = [
            _step("先读取日志", section="阶段"),
            _step("其他步骤", section="中间"),
            _step("然后写报告", section="阶段"),
        ]
        assert detect_order_pairs(insts) == []

    def test_non_heading_step_does_not_break_adjacency(self) -> None:
        # 子序列口径:中间插一条 CONSTRAINT 不截断 HEADING_STEP 相邻性
        insts = [_step("先读取输入日志"), _constraint("禁止编造数据"), _step("然后分类错误")]
        assert len(detect_order_pairs(insts)) == 1

    def test_excerpt_used_for_detection(self) -> None:
        # 连词在被切走的 bullet 前缀(只进 source_excerpt)——检测文本用 source_excerpt
        a = _step("读取输入日志", excerpt="先读取输入日志文件本身")
        b = _step("分类错误", excerpt="分类错误并记录")
        assert len(detect_order_pairs([a, b])) == 1


# ---------------------------------------------------------------------------
# detect_quantity:count / distributive 两型 + 检测文本口径
# ---------------------------------------------------------------------------


class TestDetectQuantity:
    def _one(self, insts):
        out = detect_quantity(insts)
        assert len(out) == 1
        return out[0][1]

    def test_gte(self) -> None:
        assert self._one([_constraint("至少 2 次")]) == QuantitySpec("count", "gte", 2)
        # 自然语序:操作词与数字之间有动词(「至少**调用** 2 次」)
        assert self._one([_constraint("至少调用 2 次校验")]) == QuantitySpec("count", "gte", 2)
        assert self._one([_constraint("重试不少于 3 次")]) == QuantitySpec("count", "gte", 3)

    def test_gte_without_unit_not_count(self) -> None:
        # 裸数字无「次/遍」单位不可程序比对(「不少于 3」数的什么?),不标 count
        assert detect_quantity([_constraint("校验点不少于 3")]) == []

    def test_lte(self) -> None:
        assert self._one([_constraint("重试至多 5 次")]) == QuantitySpec("count", "lte", 5)
        assert self._one([_constraint("不超过 4 次调用")]) == QuantitySpec("count", "lte", 4)

    def test_eq(self) -> None:
        assert self._one([_constraint("恰好执行 2 次")]) == QuantitySpec("count", "eq", 2)
        assert self._one([_constraint("运行测试 3 次")]) == QuantitySpec("count", "eq", 3)
        assert self._one([_constraint("跑 2 遍")]) == QuantitySpec("count", "eq", 2)

    def test_distributive(self) -> None:
        for text in ["必须为报告的每个错误引用原文", "所有错误都要分类", "逐个核对条目", "各一份副本", "every error must be cited", "all rows", "each item"]:
            assert self._one([_constraint(text)]) == QuantitySpec("distributive"), text

    def test_detection_text_uses_source_excerpt(self) -> None:
        # 动机案例:TOOL_REQUIREMENT 的 instruction_text 是「动词+工具」片段,数量词在被切
        # 走的前缀里(source_excerpt)——只测 instruction_text 会漏主力。
        inst = _tool_req("读取 references/error-codes.md", "Read",
                         excerpt="分类每个错误 — 读取 references/error-codes.md")
        assert self._one([inst]) == QuantitySpec("distributive")

    def test_chinese_numeral_not_supported(self) -> None:
        # 首期只认 ASCII 数字:「至少两次」不命中(记档 spec「明确不做」)
        assert detect_quantity([_constraint("至少两次重试")]) == []

    def test_bare_digit_not_count(self) -> None:
        # 裸数字无单位不算 count 型(避免正文任意数字误命中)
        assert detect_quantity([_constraint("报告共 3 个章节")]) == []

    def test_output_format_skipped(self) -> None:
        inst = InstructionCheck(
            source=InstructionSource.OUTPUT_FORMAT,
            instruction_text="每个错误一行",
            section="输出",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.PATTERN_MATCH,
            params={"sample": "x"},
            source_excerpt="每个错误一行",
        )
        assert detect_quantity([inst]) == []  # output 桶走确定性子串匹配,不移交

    def test_no_quantity_words(self) -> None:
        assert detect_quantity([_constraint("禁止编造数据"), _step("读取输入日志")]) == []

    def test_neighbor_excerpt_does_not_leak(self) -> None:
        # body 级指令的 excerpt = nearest preceding bullet;不含本条指令文时不得拿邻条
        # 文本当本条要求——邻条有「每个」不传染给本条(误移交 = 语义错位的判定)。
        inst = _constraint("禁止编造数据。", excerpt="写报告 — 含每个错误一行")
        assert detect_quantity([inst]) == []


# ---------------------------------------------------------------------------
# count_tool_calls:数次数的 per-record 谓词(不动 match_tool_call 语义)
# ---------------------------------------------------------------------------


class TestCountToolCalls:
    def test_counts_command_word_hits(self) -> None:
        trace = [
            _tc("Bash", "ruff check --fix"),
            _tc("Bash", "ruff check src/"),
            _tc("Bash", "pytest -q"),
            _tc("Bash", "ruff format ."),
        ]
        assert count_tool_calls("ruff", trace) == 3
        assert count_tool_calls("pytest", trace) == 1

    def test_name_substring_hit(self) -> None:
        trace = [_tc("mcp__web_reader__webReader", "x"), _tc("mcp__web_reader__webReader", "y"), _tc("Bash", "z")]
        assert count_tool_calls("webReader", trace) == 2

    def test_word_boundary_not_substring(self) -> None:
        # \b:git 不得命中 cd widgets / cat concat.py
        assert count_tool_calls("git", [_tc("Bash", "cd widgets && cat concat.py")]) == 0

    def test_required_params_filter(self) -> None:
        trace = [_tc("Bash", "ruff check --fix"), _tc("Bash", "ruff check --tb=short")]
        assert count_tool_calls("ruff", trace, required_params=["--tb=short"]) == 1

    def test_empty_tool_name_or_trace(self) -> None:
        assert count_tool_calls("", [_tc("Bash", "x")]) == 0
        assert count_tool_calls("ruff", []) == 0


# ---------------------------------------------------------------------------
# _judge_control_flow:程序判先行 + LLM 判官(mock call_claude)
# ---------------------------------------------------------------------------


def _pass_fake(response: str):
    captured: dict = {}

    def fake(prompt, *a, **k):
        captured.setdefault("prompts", []).append(prompt)
        return response

    return fake, captured


class TestJudgeControlFlowProgrammatic:
    def test_count_below_threshold_fails(self) -> None:
        aud = TranscriptAuditor()
        inst = _tool_req("运行 ruff check 至少 2 次", "ruff")
        quantified = [(inst, QuantitySpec("count", "gte", 2))]
        findings = aud._judge_control_flow(
            [], quantified, "执行记录", [_tc("Bash", "ruff check src/")], "s.jsonl",
        )
        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.method == AuditMethod.PROGRAMMATIC
        assert f.category == AuditCategory.CONDITIONAL
        assert f.control_flow == "quantity"
        assert "实测" in f.evidence and "1" in f.evidence

    def test_count_meets_threshold_passes(self) -> None:
        aud = TranscriptAuditor()
        inst = _tool_req("运行 ruff check 至少 2 次", "ruff")
        trace = [_tc("Bash", "ruff check a"), _tc("Bash", "ruff check b"), _tc("Bash", "ruff check c")]
        findings = aud._judge_control_flow(
            [], [(inst, QuantitySpec("count", "gte", 2))], "执行记录", trace, "s.jsonl",
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].method == AuditMethod.PROGRAMMATIC
        assert findings[0].control_flow == "quantity"
        assert "3" in findings[0].evidence

    def test_programmatic_only_no_llm_call(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("程序判路径不应调 LLM"),
        )
        aud = TranscriptAuditor()
        inst = _tool_req("运行 ruff check 恰好 1 次", "ruff")
        findings = aud._judge_control_flow(
            [], [(inst, QuantitySpec("count", "eq", 1))], "执行记录",
            [_tc("Bash", "ruff check")], "s.jsonl",
        )
        assert findings[0].verdict == AuditVerdict.PASS

    def test_count_without_tool_name_goes_llm(self, monkeypatch) -> None:
        # count 型但无工具映射 → LLM 路(覆盖面窄的诚实定位:程序判不是主力)
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "3 个错误仅 2 个有引用"}]',
        )
        aud = TranscriptAuditor()
        inst = _constraint("至少覆盖 2 个模块")
        findings = aud._judge_control_flow(
            [], [(inst, QuantitySpec("count", "gte", 2))], "执行记录", [], "s.jsonl",
        )
        assert findings[0].verdict == AuditVerdict.FAIL
        assert findings[0].method == AuditMethod.LLM
        assert findings[0].category == AuditCategory.PROHIBITION
        assert findings[0].control_flow == "quantity"

    def test_count_ignores_misparsed_required_params(self) -> None:
        """数量程序判不带参数断言:数量词里的数字被 extractor 误抽成 required_params
        (「至少 2 次」→ ['2'])时,空 input 的真实调用仍要计数——db-migration
        fixture 真跑实证的误判形态(调了 2 次却被断言滤成 0 次误 FAIL)。"""
        aud = TranscriptAuditor()
        inst = _tool_req("运行 ruff check 至少 2 次", "ruff")
        inst.params["required_params"] = ["2"]  # 复现 extractor 误抽形态
        findings = aud._judge_control_flow(
            [], [(inst, QuantitySpec("count", "gte", 2))], "执行记录",
            [_tc("Bash", "ruff check a"), _tc("Bash", "ruff check b")], "s.jsonl",
        )
        assert findings[0].verdict == AuditVerdict.PASS
        assert findings[0].evidence == "实测调用 ruff 2 次,满足「至少 2 次」"


class TestJudgeControlFlowLLM:
    def _pair(self) -> OrderPair:
        return OrderPair(
            _step("先读取输入日志", headline="读日志"),
            _step("然后分类错误", headline="分类"),
        )

    def test_order_fail_synthetic_finding(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "分类先于读取"}]',
        )
        aud = TranscriptAuditor()
        findings = aud._judge_control_flow([self._pair()], [], "执行记录", [], "s.jsonl")
        f = findings[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.synthetic is True
        assert f.category == AuditCategory.STEP
        assert f.source == "HEADING_STEP"
        assert f.instruction_text == "(顺序)「读日志」须先于「分类」"  # headline 优先
        assert f.control_flow == ""  # ORDER 靠 synthetic+(顺序) 文案自明,不另标
        assert f.related == []  # related 由 audit() 回填,判官不设
        assert f.turn_refs == []  # locate_turns 默认关 → 即便 LLM 给了也丢

    def test_order_headline_fallback_to_instruction_text(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "na", "evidence": "B 未发生"}]',
        )
        aud = TranscriptAuditor()
        pair = OrderPair(_step("先读取输入日志"), _step("然后分类错误"))  # headline 空
        f = aud._judge_control_flow([pair], [], "执行记录", [], "s.jsonl")[0]
        assert f.instruction_text == "(顺序)「先读取输入日志」须先于「然后分类错误」"
        assert f.verdict == AuditVerdict.NA

    def test_order_indeterminate_with_evidence(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "indeterminate", "evidence": "两步证据时间戳矛盾"}]',
        )
        f = TranscriptAuditor()._judge_control_flow([self._pair()], [], "x", [], "s.jsonl")[0]
        assert f.verdict == AuditVerdict.INDETERMINATE

    def test_quantity_unresolved_keeps_marker(self, monkeypatch) -> None:
        """数量指令 LLM 没出判定(缺 verdict)→ UNRESOLVED 也带 quantity 记号。

        记号是纯显示,但别在「最需要人复核的没判成条目」上失去识别度——CLI/HTML
        靠它挂「数量」徽章,与 PASS/FAIL 路径的记号口径须一致。
        """
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "evidence": ""}]',
        )
        aud = TranscriptAuditor()
        inst = _constraint("每个错误都要引用对应的日志原文行。")
        f = aud._judge_control_flow(
            [], [(inst, QuantitySpec("distributive"))], "执行记录", [], "s.jsonl",
        )[0]
        assert f.verdict == AuditVerdict.UNRESOLVED
        assert f.control_flow == "quantity"

    def test_order_indeterminate_without_evidence_downgrades(self, monkeypatch) -> None:
        # 滥用闸(镜像步判官):indeterminate 无 evidence → UNRESOLVED,不是 INDETERMINATE
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "indeterminate", "evidence": ""}]',
        )
        f = TranscriptAuditor()._judge_control_flow([self._pair()], [], "x", [], "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED

    def test_unknown_verdict_unresolved_not_na(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "maybe"}]',
        )
        f = TranscriptAuditor()._judge_control_flow([self._pair()], [], "x", [], "s.jsonl")[0]
        assert f.verdict == AuditVerdict.UNRESOLVED  # 没判成不落 na(假 N/A)

    def test_quantity_fail_keeps_original_category(self, monkeypatch) -> None:
        # 移交的 CONSTRAINT:finding 沿用原指令全文 + 原桶类别,不是 synthetic
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "3 个错误仅 2 个有引用"}]',
        )
        aud = TranscriptAuditor()
        inst = _constraint("必须为报告的每个错误引用对应的日志原文行")
        f = aud._judge_control_flow([], [(inst, QuantitySpec("distributive"))], "x", [], "s.jsonl")[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.instruction_text == inst.instruction_text
        assert f.category == AuditCategory.PROHIBITION
        assert f.synthetic is False

    def test_prompt_contains_pair_bullets_and_tool_section(self, monkeypatch) -> None:
        fake, captured = _pass_fake('[{"index": 0, "verdict": "pass", "evidence": "ok"}]')
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        pair = OrderPair(
            _step("先读取输入日志", excerpt="先读取输入日志文件本身"),
            _step("然后分类错误", excerpt="然后分类每个错误"),
        )
        # tool_section 渲染要求 attr 风格(同 step 判官的现有约定)
        trace = [SimpleNamespace(name="Read", input_summary='{"file_path": "/x/app.log"}')]
        TranscriptAuditor()._judge_control_flow([pair], [], "执行记录", trace, "s.jsonl")
        prompt = captured["prompts"][0]
        assert "先读取输入日志文件本身" in prompt  # source_excerpt 优先(含主语)
        assert "先做" in prompt and "后做" in prompt
        assert "## 实际工具调用记录" in prompt and "app.log" in prompt

    def test_tool_section_anchors_with_locate_turns(self, monkeypatch) -> None:
        fake, captured = _pass_fake('[{"index": 0, "verdict": "pass", "evidence": "ok"}]')
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        trace = [SimpleNamespace(name="Read", input_summary="app.log", turn=3)]
        TranscriptAuditor()._judge_control_flow([self._pair()], [], "x", trace, "s.jsonl", locate_turns=True)
        assert "[T3]" in captured["prompts"][0]

    def test_turn_refs_sanitized_on_fail_with_locate_turns(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "违序", "turn_refs": [2, 99, -1]}]',
        )
        aud = TranscriptAuditor()
        f = aud._judge_control_flow(
            [self._pair()], [], "[T1] a\n[T2] b\n[T3] c", [], "s.jsonl", locate_turns=True,
        )[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.turn_refs == [2]  # 上界 3 内保留,99/-1 丢

    def test_turn_refs_dropped_when_locate_turns_off(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "verdict": "fail", "evidence": "违序", "turn_refs": [2]}]',
        )
        f = TranscriptAuditor()._judge_control_flow(
            [self._pair()], [], "[T1] a\n[T2] b", [], "s.jsonl", locate_turns=False,
        )[0]
        assert f.verdict == AuditVerdict.FAIL
        assert f.turn_refs == []  # 整链开关:关时即便 LLM 给了也丢

    def test_batch_failure_unresolved(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        aud = TranscriptAuditor()
        findings = aud._judge_control_flow([self._pair()], [], "x", [], "s.jsonl")
        assert findings[0].verdict == AuditVerdict.UNRESOLVED

    def test_empty_inputs_no_llm(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: pytest.fail("空输入不应调 LLM"),
        )
        assert TranscriptAuditor()._judge_control_flow([], [], "x", [], "s.jsonl") == []


# ---------------------------------------------------------------------------
# synthetic 下游:link_related 跳过 / 噪声豁免 / 聚合排除
# ---------------------------------------------------------------------------


def _mk_finding(**kw) -> AuditFinding:
    defaults = dict(
        instruction_text="读取日志", source_excerpt="", headline="", section="步骤",
        source="HEADING_STEP", verdict=AuditVerdict.FAIL, method=AuditMethod.LLM,
        category=AuditCategory.STEP, evidence="证据", transcript="s.jsonl",
    )
    defaults.update(kw)
    return AuditFinding(**defaults)


class TestSyntheticDownstream:
    def test_link_related_skips_synthetic_both_ways(self) -> None:
        # ORDER 合成文本内嵌 headline 带文件名 → 与真实 finding 共享 ref token;
        # 不跳过则真实 finding 被链向合成 finding(单向悬链)。
        real = _mk_finding(instruction_text="读取 references/error-codes.md")
        synth = _mk_finding(
            instruction_text='(顺序)「读取 references/error-codes.md」须先于「分类」',
            synthetic=True,
        )
        findings = assign_sequence([real, synth])
        link_related(findings)
        assert real.related == [], "真实 finding 不应被链向 synthetic"
        assert synth.related == [], "synthetic 不参与 Case B 关联(related 由回填)"

    def test_link_related_real_pair_still_works(self) -> None:
        # 控制组:两条真实 finding 共享 token 照常互链(跳过 synthetic 不破坏既有行为)
        a = _mk_finding(instruction_text="读取 references/error-codes.md")
        b = _mk_finding(instruction_text="对照 references/error-codes.md 校验", category=AuditCategory.CONDITIONAL)
        findings = assign_sequence([a, b])
        link_related(findings)
        assert a.related == ["#2"] and b.related == ["#1"]

    def test_noise_exempt_synthetic(self) -> None:
        # 启发式 3(LLM+related → tool_noise)在 ORDER 上语义是 pair 两端,豁免;
        # 对照组:真实 LLM+related FAIL 照标 tool_noise。
        synth = _mk_finding(instruction_text="(顺序)「A」须先于「B」", related=["#3"], synthetic=True)
        real = _mk_finding(related=["#3"])
        annotate_noise([synth, real])
        assert synth.noise_label is None
        assert real.noise_label == "tool_noise"

    def test_aggregate_by_instruction_skips_synthetic(self) -> None:
        synth = _mk_finding(instruction_text="(顺序)「A」须先于「B」", synthetic=True)
        real = _mk_finding(instruction_text="读取日志", verdict=AuditVerdict.PASS)
        rows = aggregate_by_instruction([synth, real])
        assert [r.instruction_text for r in rows] == ["读取日志"]

    def test_aggregate_coverage_skips_synthetic(self) -> None:
        synth = _mk_finding(instruction_text="(顺序)「A」须先于「B」", synthetic=True, verdict=AuditVerdict.NA)
        real = _mk_finding(instruction_text="读取日志", verdict=AuditVerdict.PASS)
        cov = aggregate_coverage([synth, real])
        assert cov.total == 1
        assert all(row.instruction_text != synth.instruction_text
                   for sec in cov.sections for row in sec.instructions)


# ---------------------------------------------------------------------------
# audit() 接线:移交路由 / related 回填 / 异常兜底 / 零行为变化
# ---------------------------------------------------------------------------

# 合成 SKILL.md:步骤两编号行(先…/然后…,2 带「每个」)+ 规则两条(每有一条移交、
# 禁止编造留禁令桶——注意它被 CONSTRAINT+ANTI_FABRICATION 双抽,dedup 并 1)。
# 控制流信号:1 pair + 2 条数量移交。CONSTRAINT 的 instruction_text 保留句号(extractor 原样)。
CF_SKILL_BODY = (
    "## 步骤\n\n"
    "1. 先读取输入日志文件\n"
    "2. 然后分类每个错误并记录\n\n"
    "## 规则\n\n"
    "必须为报告的每个错误引用对应的日志原文行。\n"
    "禁止编造数据。\n"
)

# 无任何控制流信号的 skill(零行为变化对照)。步骤行 ≥5 字(HEADING_STEP 下限)。
PLAIN_SKILL_BODY = (
    "## 步骤\n\n"
    "1. 读取输入日志文件\n"
    "2. 分类错误类型\n\n"
    "## 规则\n\n"
    "禁止编造数据。\n"
)


def _blanket_fake():
    """通用 mock:禁令 violated=true、步骤/控制流 verdict=pass(兼容三判官 + 控制流)。"""
    calls: list[str] = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        return json.dumps([
            {"index": i, "applicable": False, "violated": True,
             "verdict": "pass", "evidence": "mock 判定"}
            for i in range(10)
        ])

    return fake, calls


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


@pytest.fixture()
def one_transcript(tmp_path) -> Path:
    return _write_jsonl(tmp_path / "session.jsonl", [
        {"message": {"role": "user", "content": "分析这个日志"}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "读取并分类了错误,写好报告"}]}},
    ])


class TestAuditControlFlowWiring:
    def test_quantity_prompt_pins_completion_to_record(self, tmp_path, monkeypatch, one_transcript) -> None:
        """数量判据含「完成度以本执行记录为全集」规则:禁猜切片外继续,完成度主判。

        实锤:「逐一完成 Tasks」被判官读成顺序合规、拿记录内顺序掩盖 32/50 数量不足
        → PASS 摆动。对账对象就是这份记录,判官不得推测记录之外的事。
        """
        fake, prompts = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        TranscriptAuditor().audit("cf-skill", CF_SKILL_BODY, [one_transcript])

        cf_prompts = [p for p in prompts if "顺序/数量要求" in p]
        assert cf_prompts, "控制流判官 prompt 未被调用"
        p = cf_prompts[0]
        assert "以本执行记录为全集" in p
        assert "不得推测「记录之外可能继续」" in p
        assert "完成度是主判据" in p

    def test_quantity_prompt_has_window_and_condition_boundary(self, tmp_path, monkeypatch, one_transcript) -> None:
        """数量判据含「窗口与条件边界」:阶段从未开始/条件未触发的单条任务级条目判 na。

        实锤:完成度条款落地后,「据 Task 35 失败报告修复」类条件触发型条目(其所属
        阶段全程未开始、触发条件从未发生)被「应做未做都算 fail/不得把没做洗成 na」
        压成 FAIL——窗口语义此前只写进步骤判官,CF 判官没拿到。边界须带反向闸
        (阶段已开始、中途没走仍 fail),且与完成度口径互认:整体缺口由完成度条目
        兜底,单条条目判 na 不算漏报。
        """
        fake, prompts = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        TranscriptAuditor().audit("cf-skill", CF_SKILL_BODY, [one_transcript])

        cf_prompts = [p for p in prompts if "顺序/数量要求" in p]
        assert cf_prompts, "控制流判官 prompt 未被调用"
        p = cf_prompts[0]
        assert "窗口与条件边界" in p
        assert "从未开始" in p
        assert "条件未触发" in p
        assert "触发条件在记录里从未发生" in p
        assert "仍判 fail,不得借本边界洗成 na" in p
        assert "单条任务级条目按「窗口与条件边界」判 na,不算漏报" in p
        assert "不得推测「记录之外可能继续」" in p
        assert "完成度是主判据" in p

    def test_end_to_end_order_and_quantity_handoff(self, tmp_path, monkeypatch, one_transcript) -> None:
        fake, prompts = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        report = TranscriptAuditor().audit("cf-skill", CF_SKILL_BODY, [one_transcript])

        # ORDER finding:合成文案、synthetic、PASS、related 指向两端真实 finding 的 seq
        order = [f for f in report.findings if f.synthetic]
        assert len(order) == 1
        of = order[0]
        assert of.verdict == AuditVerdict.PASS
        assert of.instruction_text.startswith("(顺序)")
        assert of.category == AuditCategory.STEP
        assert len(of.related) == 2
        for ref in of.related:
            target = next(f for f in report.findings if f.seq == int(ref[1:]))
            assert target.synthetic is False
            assert target.instruction_text in ("先读取输入日志文件", "然后分类每个错误并记录")

        # 数量移交:B(HEADING_STEP 带「每个」)与 CONSTRAINT(每个)都由控制流判官判,
        # verdict 照常落位且不是 synthetic
        q_step = next(f for f in report.findings
                      if f.instruction_text == "然后分类每个错误并记录")
        q_constraint = next(f for f in report.findings
                            if f.instruction_text == "必须为报告的每个错误引用对应的日志原文行。")
        assert q_step.synthetic is False and q_constraint.synthetic is False
        assert q_step.verdict == AuditVerdict.PASS

        # 移交不进原判官桶:控制流 prompt 含两条数量指令,禁令 prompt 不含 CONSTRAINT 移交件
        cf_prompt = next(p for p in prompts if "先做" in p)
        assert "然后分类每个错误并记录" in cf_prompt
        assert "必须为报告的每个错误引用对应的日志原文行" in cf_prompt
        proh_prompt = next(p for p in prompts if "禁令" in p)
        assert "必须为报告的每个错误引用对应的日志原文行" not in proh_prompt

        # 聚合透明:by_instruction/coverage 无合成文案;summary 含 ORDER
        assert all(not r.instruction_text.startswith("(顺序)") for r in report.by_instruction)
        assert all(not row.instruction_text.startswith("(顺序)")
                   for sec in report.coverage.sections for row in sec.instructions)
        # findings:A、B、CONSTRAINT×2(每有=PASS / 禁止编造=violated true=FAIL)+ ORDER
        assert report.summary["total"] == 5
        assert report.summary["pass"] == 4
        assert report.summary["fail"] == 1

    def test_related_lookup_uses_original_text_not_headline(
            self, tmp_path, monkeypatch, one_transcript) -> None:
        # headline 生成后,ORDER 合成文案用 headline、回填查找仍按原始 instruction_text——
        # 按 headline 查会全部落空、ORDER 整体静默不产(spec 二审 I-5)。
        fake, _ = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        monkeypatch.setattr(
            "sift.audit.auditor.headline_cache.get_or_compute",
            lambda key, fn: ["标题甲", "标题乙", "标题丙", "标题丁"],
        )
        report = TranscriptAuditor().audit(
            "cf-skill", CF_SKILL_BODY, [one_transcript], generate_headline=True,
        )
        order = [f for f in report.findings if f.synthetic]
        assert len(order) == 1
        assert "标题甲" in order[0].instruction_text  # 合成文案用 headline
        assert len(order[0].related) == 2  # 回填仍成功(按原始文本查)

    def test_endpoint_missing_drops_order_finding(self, tmp_path, monkeypatch, one_transcript) -> None:
        # 幽灵端点(不在 skill 里)→ 该 ORDER finding 丢弃不产;seq 跳号无害
        fake, _ = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        ghost_pair = OrderPair(_step("幽灵步骤不存在"), _step("然后分类每个错误并记录"))
        monkeypatch.setattr("sift.audit.auditor.detect_order_pairs", lambda insts: [ghost_pair])
        monkeypatch.setattr("sift.audit.auditor.detect_quantity", lambda insts: [])
        report = TranscriptAuditor().audit("cf-skill", CF_SKILL_BODY, [one_transcript])
        assert all(not f.synthetic for f in report.findings)

    def test_judge_exception_fallback_unresolved(self, tmp_path, monkeypatch, one_transcript) -> None:
        # 判官整体异常 → pairs + 移交指令全兜底 UNRESOLVED(cause=judge_error),audit 不崩
        def boom(self, *a, **k):
            raise RuntimeError("cf judge down")

        # mock 三判官:本测试只断言 CF 兜底产物,别让 refine/条件性/禁令/步骤真调
        # 后端(有凭据的开发机上每次跑测试都烧真实 token,实测单用例 7.85s→0.14s)
        monkeypatch.setattr(
            "sift.audit.auditor.call_claude",
            lambda *a, **k: '[{"index": 0, "applicable": false, "violated": false,'
                            ' "verdict": "pass", "evidence": "mock"}]',
        )
        monkeypatch.setattr(TranscriptAuditor, "_judge_control_flow", boom)
        report = TranscriptAuditor().audit("cf-skill", CF_SKILL_BODY, [one_transcript])
        synth_unresolved = [f for f in report.findings
                            if f.synthetic and f.verdict == AuditVerdict.UNRESOLVED]
        assert len(synth_unresolved) == 1
        assert synth_unresolved[0].unresolved_cause == "judge_error"
        q = [f for f in report.findings
             if f.instruction_text == "必须为报告的每个错误引用对应的日志原文行。"]
        assert q and q[0].verdict == AuditVerdict.UNRESOLVED
        assert q[0].unresolved_cause == "judge_error"
        # 兜底 UNRESOLVED 也带 quantity 记号:最需要人复核的没判成条目得认得出是数量判定
        assert q[0].control_flow == "quantity"

    def test_no_control_flow_signal_zero_behavior_change(
            self, tmp_path, monkeypatch, one_transcript) -> None:
        # 无数量词/顺序词的声明:控制流判官不触发(零新调用),audit 照常
        def sentinel(self, *a, **k):
            raise AssertionError("无控制流信号不应调 _judge_control_flow")

        monkeypatch.setattr(TranscriptAuditor, "_judge_control_flow", sentinel)
        fake, calls = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        report = TranscriptAuditor().audit("plain-skill", PLAIN_SKILL_BODY, [one_transcript])
        assert all(not f.synthetic for f in report.findings)
        assert report.summary["total"] >= 3  # 2 步骤 + 1 禁令照常判


class TestDbMigrationFixture:
    """committed fixture(example-skills/db-migration-zh)守恒:真实 SKILL.md 的
    检测面 + 违规 transcript 的确定性程序判输入。fixture 被改坏(顺序词/数量词
    被删、transcript 换内容)时这里先红,防「fixture 在但检测面已空」的静默腐化。"""

    BASE = Path(__file__).parent.parent / "fixtures" / "example-skills" / "db-migration-zh"

    def test_declaration_detection_surface(self) -> None:
        from sift.compliance.extractor import InstructionExtractor
        from sift.parser import parse_skill_md

        _, _, _, _, body = parse_skill_md(self.BASE)
        insts = InstructionExtractor().extract(body)
        # 顺序对:备份→迁移、迁移→校验(同 section 相邻,显式连词)
        pairs = detect_order_pairs(insts)
        assert len(pairs) == 2
        assert "备份" in pairs[0].a.instruction_text
        assert "迁移" in pairs[0].b.instruction_text
        # 数量指令恰一条:count/gte/2 + tool_name(程序判输入);裸行写法防
        # HEADING_STEP/TOOL_REQUIREMENT 同 bullet 双抽双计
        quants = detect_quantity(insts)
        assert [(q[0].params.get("tool_name"), q[1].kind, q[1].op, q[1].threshold)
                for q in quants] == [("health-check", "count", "gte", 2)]

    def test_violation_transcript_programmatic_fail(self, monkeypatch) -> None:
        from sift.audit.rebuild import rebuild
        from sift.compliance.extractor import InstructionExtractor
        from sift.parser import parse_skill_md

        tp = self.BASE / "transcripts" / "synthetic-violation.jsonl"
        # 确定性输入:health-check 实调 1 次 < 2;Bash×3(迁移/校验/备份倒序)
        trace, _, _ = rebuild(tp)
        assert count_tool_calls("health-check", trace) == 1
        assert [t.name for t in trace] == ["Bash", "Bash", "Bash", "health-check"]

        fake, _ = _blanket_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        _, _, _, _, body = parse_skill_md(self.BASE)
        report = TranscriptAuditor().audit("db-migration-zh", body, [tp])
        # 程序判 FAIL(method=PROGRAMMATIC)不依赖 LLM;ORDER synthetic 照常产
        q = [f for f in report.findings
             if "health-check" in (f.instruction_text or "") and not f.synthetic]
        assert q and q[0].verdict == AuditVerdict.FAIL
        assert q[0].method == AuditMethod.PROGRAMMATIC
        assert q[0].control_flow == "quantity"
        assert len([f for f in report.findings if f.synthetic]) == 2


def test_issue_triage_agent_fixture_order_surface() -> None:
    """agent 侧 fixture(example-agents/issue-triage-agent)守恒:原生 .md 声明的
    步骤节同样产顺序对(控制流判官不绑 skill 形态)。"""
    from sift.compliance.extractor import InstructionExtractor
    from sift.parser import parse_skill_md

    base = (Path(__file__).parent.parent / "fixtures" / "example-agents"
            / "issue-triage-agent" / "issue-triage-agent.md")
    _, _, _, _, body = parse_skill_md(base)
    pairs = detect_order_pairs(InstructionExtractor().extract(body))
    assert len(pairs) == 2
    assert "检索" in pairs[0].a.instruction_text and "归类" in pairs[0].b.instruction_text
