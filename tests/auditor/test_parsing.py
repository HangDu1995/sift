"""Auditor parsing behavior tests."""

from __future__ import annotations

import json
import re

from sift.audit.auditor import (
    TranscriptAuditor,
    parse_applicability_results,
    parse_prohibition_results,
    parse_step_results,
)
from sift.audit.judge_context import (
    AuditOptions,
    BatchJudgeOptions,
    JudgeBatch,
    JudgeCallOptions,
    JudgePromptContext,
    JudgeTranscript,
)
from sift.llm_transport import LLMCallConfig
from sift.models import InstructionCheck
from sift.models.audit_models import (
    AuditVerdict,
)
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)

from .helpers import (
    _headline_fake,
    _pass_fake,
    _prohibition_inst,
    _read_file_records,
    _step_inst,
    _tool_inst,
    _write_jsonl,
)


def test_parse_step_recovers_array_trailing_comma() -> None:
    """LLM 输出数组尾逗号([... ,])→ 旧逻辑硬失败整批没判成;现应救回 pass/fail。"""
    text = '[{"index":0,"verdict":"pass"},{"index":1,"verdict":"fail"},]'
    res = parse_step_results(text, 2)
    assert res[0][0] == AuditVerdict.PASS
    assert res[1][0] == AuditVerdict.FAIL


def test_parse_prohibition_recovers_object_trailing_comma() -> None:
    """对象内尾逗号({...,})同样兜底;禁令 violated=true 不该因尾逗号漏判成 UNRESOLVED。"""
    text = '[{"index":0,"violated":true,"evidence":"编造了数据",},]'
    res = parse_prohibition_results(text, 1)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[0][1] == "编造了数据"


def test_parse_applicable_recovers_trailing_comma() -> None:
    """条件性 applicable 判定同样兜底;true→FAIL(该做没做)/false→NA(不涉及)。"""
    text = '[{"index":0,"applicable":true},{"index":1,"applicable":false},]'
    res = parse_applicability_results(text, 2)
    assert res[0][0] == AuditVerdict.FAIL
    assert res[1][0] == AuditVerdict.NA


def test_load_json_lenient_clean_unchanged_and_garbage_none() -> None:
    """干净 JSON 直通不跑 strip(含合法 ', ]' 的字符串值不被破坏);真垃圾 → None。"""
    from sift.audit.tool_match import load_json_lenient

    assert load_json_lenient("[1, 2, 3]") == [1, 2, 3]
    # 含 ", ]" 的合法字符串值:直接 parse 成功 → strip 不跑 → 原样保留
    assert load_json_lenient('["a, ]", "b"]') == ["a, ]", "b"]
    # 尾逗号 → 直接失败 → strip 重试 → 救回
    assert load_json_lenient("[1, 2, 3,]") == [1, 2, 3]
    # 真垃圾 → None(调用方兜底默认 / UNRESOLVED)
    assert load_json_lenient("not json at all") is None


def test_window_short_text_unchanged() -> None:
    """短文本(<= limit)原样返回;恰好 limit 不截(边界)。"""
    from sift.audit.auditor import _SCENE_TRUNCATE, _window

    assert _window("短文本") == "短文本"
    assert _window("x" * _SCENE_TRUNCATE) == "x" * _SCENE_TRUNCATE


def test_window_long_text_keeps_head_tail_and_marker() -> None:
    """长文本取头+尾(各 limit/2)+ 中段截断标记;头尾 canary 都在(旧裸 [:limit] 会丢尾)。"""
    from sift.audit.auditor import _SCENE_TRUNCATE, _window

    text = "CANARY_HEAD" + "h" * 9000 + "t" * 9000 + "CANARY_TAIL"
    out = _window(text)
    assert "CANARY_HEAD" in out  # 头保留
    assert "CANARY_TAIL" in out  # 尾保留(旧 [:8000] 只有头,丢这个)
    assert "截断" in out  # 中段标记
    assert len(out) < len(text)  # 确实缩短
    half = _SCENE_TRUNCATE // 2
    assert "CANARY_HEAD" in out[: half + 20]  # 头 canary 在前半窗
    assert "CANARY_TAIL" in out[-(half + 20):]  # 尾 canary 在后半窗


def test_prohibition_long_transcript_uses_head_tail_window(monkeypatch) -> None:
    """长 transcript_text(>8000):prohibition prompt 含头+尾 canary + 截断标记。"""
    captured: dict = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return "[]"

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    transcript_auditor = TranscriptAuditor()
    # ~18000 字:尾 canary 在 [:8000] 之外 → 旧裸切会丢
    transcript_text = "CANARY_HEAD_PART" + "h" * 9000 + "t" * 9000 + "CANARY_TAIL_PART"
    transcript_auditor.engine.judge_prohibition(
        instructions=[_prohibition_inst()],
        evidence=JudgeTranscript(text=transcript_text, name="p.jsonl"),
        call_options=JudgeCallOptions(),
    )

    assert "CANARY_HEAD_PART" in captured["prompt"]
    assert "CANARY_TAIL_PART" in captured["prompt"]  # 旧 [:8000] 实现会缺
    assert "截断" in captured["prompt"]


def test_conditional_scene_fallback_when_no_user_turn(tmp_path, monkeypatch) -> None:
    """skill/agent 切片无 user turn → conditional 判官原拿空场景全弃判(实测主噪声源);

    回退用 transcript_text(执行记录)当场景 → prompt 场景描述含执行 canary、非空,
    判官方能判 applicability 而非一律 INDETERMINATE。
    """
    captured: dict = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return json.dumps([{"index": i, "applicable": False} for i in range(60)])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    # 无 user turn 的切片:仅 assistant 文字(canary)+ 一个不匹配 "use Bash" 的 Read
    tp = _write_jsonl(
        tmp_path / "slice.jsonl",
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "执行中 CANARY_SCENE_FALLBACK 标记。"},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x/foo.md"}},
                    ],
                }
            },
        ],
    )
    TranscriptAuditor().audit(
        skill_name="s", skill_body="use Bash", transcripts=[tp], options=AuditOptions()
    )  # use Bash → conditional, 不匹配 Read → LLM

    p = captured["prompt"]
    assert "## 场景描述" in p
    m = re.search(r"## 场景描述\n(.*?)\n\n## 待判指令", p, re.S)
    assert m, "未找到 conditional prompt 的场景描述段"
    scene = m.group(1)
    assert scene.strip() != "", "skill 切片无 user turn → 场景描述不应为空(应回退 transcript_text)"
    assert "CANARY_SCENE_FALLBACK" in scene


def test_source_excerpt_propagates_to_finding(tmp_path, monkeypatch):
    """audit 产出的 finding 带上 instruction 的 source_excerpt(主语上下文)。

    判定不变:judge 仍吃 instruction_text(谓语片段);source_excerpt 只透传给 finding
    供显示层补全被切的主语。回归:st-verifier「必须存在且非空」无主语(主语在列表项
    首行被切走)→ finding.source_excerpt 带回 design_doc_cases.json。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    body = (
        "## 标准产物存在性\n\n"
        "- **用例生成类**:`design_doc_cases.json` 与 `ttk_design_doc_cases.csv`\n"
        "  (黑盒为其 TTK 用例 CSV)必须存在且非空 —— 缺任一 → FAILED。\n"
    )
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

    must = [f for f in report.findings if "必须存在" in f.instruction_text and "非空" in f.instruction_text]
    assert must, "应有「必须存在且非空」的 finding"
    # 主语从列表项首行经 instruction → finding 透传
    assert any("design_doc_cases.json" in f.source_excerpt for f in must), (
        "source_excerpt 应带回主语 design_doc_cases.json"
    )
    # 判定用的 instruction_text 仍是谓语片段(主语不在其中)
    assert all("design_doc_cases.json" not in f.instruction_text for f in must)


def test_derived_text_collision_with_static_instruction(tmp_path, monkeypatch):
    """derived 反推文案与静态声明指令同文 → 静态指令不得被误标 derived。

    回归钉:trace 里绝对路径 Read 归一后,derived 反推的「读取 references/x.md」
    与声明步骤同文——按纯文本命中会把静态 HEADING_STEP finding 标成 derived,
    踢出 coverage 分母(实测 total 少 1)与 suggest 的 PASS 保留账本。同文碰撞时
    该文本按真声明指令对待(finding 无法区分出处,但静态集里存在即按静态算)。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    body = "# 步骤\n\n1. 读取 references/error-codes.md\n2. 总结错误类别\n\n## 规则\n\n禁止编造数据。\n"
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/ws/references/error-codes.md"))
    report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

    step = [finding for finding in report.findings if _is_reference_read_step(finding)]
    assert step, "静态步骤指令应有 finding"
    assert step[0].derived_from_transcript is False
    # coverage 分母里保留这条真声明指令(修复前被 derived 标记踢出)
    covered_texts = {i.instruction_text for sec in report.coverage.sections for i in sec.instructions}
    assert "读取 references/error-codes.md" in covered_texts


def test_generate_headlines_feeds_source_excerpt_and_returns_list(monkeypatch):
    """_generate_headlines 把 instruction_text + source_excerpt 喂 LLM,返回等长 headline 列表。

    判定不读 headline;空 instruction 集返回 []。LLM 据源 excerpt 补主语(测试只验管道)。
    """
    captured = {}

    def fake(prompt, *a, **k):
        captured["prompt"] = prompt
        return json.dumps([{"index": 0, "headline": "design_doc_cases.json 必须存在且非空"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
    transcript_auditor = TranscriptAuditor()
    inst = InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text="必须存在",
        section="Constraint",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        source_excerpt="用例生成类:`design_doc_cases.json` 必须存在",
    )
    headlines = transcript_auditor.engine.generate_headlines([inst], model=None, timeout=10)
    assert headlines == ["design_doc_cases.json 必须存在且非空"]
    # prompt 喂了 source_excerpt(LLM 据此补主语)
    assert "design_doc_cases.json" in captured["prompt"]
    # 空集短路,不调 LLM
    assert transcript_auditor.engine.generate_headlines([], model=None, timeout=10) == []


def test_headline_propagates_to_finding_when_enabled(tmp_path, monkeypatch):
    """generate_headline=True → audit 产出的 finding 带 headline(独立步骤,判定不变)。

    judge 仍吃 instruction_text;headline 是纯显示字段。headline_cache 指 tmp 不污染 ~/.cache。
    """
    monkeypatch.setattr("sift.audit.auditor.call_claude", _headline_fake())
    monkeypatch.setattr("sift.audit.headline_cache._cache_dir", lambda: tmp_path)
    body = "- **用例生成类**:`design_doc_cases.json` 必须存在且非空 —— 缺 → FAILED。\n"
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit(
        skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions(generate_headline=True)
    )

    must = [f for f in report.findings if "必须存在" in f.instruction_text]
    assert must, "应有「必须存在」的 finding"
    assert any(f.headline for f in must), "generate_headline=True → finding 应带 headline"


def test_headline_off_by_default(tmp_path, monkeypatch):
    """默认不生成 headline(generate_headline=False)→ finding.headline 恒空。"""
    monkeypatch.setattr("sift.audit.auditor.call_claude", _pass_fake())
    monkeypatch.setattr("sift.audit.headline_cache._cache_dir", lambda: tmp_path)
    body = "- **用例生成类**:`design_doc_cases.json` 必须存在。\n"
    tp = _write_jsonl(tmp_path / "s.jsonl", _read_file_records("/tmp/x"))
    report = TranscriptAuditor().audit(
        skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions()
    )  # 不传 generate_headline
    assert all(not f.headline for f in report.findings)


def test_refine_passes_http_transport_down_the_chain(monkeypatch):
    """refine(_refine_pseudo_instructions → _llm_classify_real)把 transport='http'
    穿到 call_claude(mock 整替 call_claude,断言收到的 transport=='http')。
    """
    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen["transport"] = transport
        # 返 is_real=true(真指令,保留一条)
        return json.dumps([{"index": 0, "is_real": True}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor().engine.refine_instructions(
        [_step_inst("Step 1: 真步骤")],
        model="glm-5.2[1m]",
        timeout=10,
    )
    assert seen.get("transport") == "http"


def test_headline_passes_http_transport_down_the_chain(monkeypatch):
    """headline(_generate_headlines)把 transport='http' 穿到 call_claude。"""
    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen["transport"] = transport
        return json.dumps([{"index": 0, "headline": "标题"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor().engine.generate_headlines(
        [_step_inst("做某事")],
        model="glm-5.2[1m]",
        timeout=10,
    )
    assert seen.get("transport") == "http"


def test_judge_applicable_passes_http_transport(monkeypatch):
    """条件性 judge(_llm_judge_applicable)走 http 通道(与 refine/headline 一致)。"""
    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen["transport"] = transport
        return json.dumps([{"index": 0, "applicable": False, "reason": "不涉及"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor().engine.judge_applicability_batches(
        candidates=[_tool_inst()],
        evidence=JudgePromptContext(text="某场景"),
        call_options=JudgeCallOptions(model="glm-5.2[1m]", timeout=10),
    )
    assert seen.get("transport") == "http"


def test_judge_prohibition_passes_http_transport(monkeypatch):
    """禁令 judge(_llm_judge_prohibition)走 http 通道。"""
    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen["transport"] = transport
        return json.dumps([{"index": 0, "violated": False, "evidence": "未违反"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor().engine.judge_prohibition_batches(
        candidates=[_prohibition_inst()],
        evidence=JudgePromptContext(text="某执行记录"),
        call_options=JudgeCallOptions(model="glm-5.2[1m]", timeout=10),
    )
    assert seen.get("transport") == "http"


def test_judge_step_passes_http_transport(monkeypatch):
    """步骤 judge(_llm_judge_step)走 http 通道。"""
    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen["transport"] = transport
        return json.dumps([{"index": 0, "verdict": "pass", "evidence": "做过"}])

    monkeypatch.setattr("sift.audit.auditor.call_claude", fake_call_claude)
    TranscriptAuditor().engine.judge_step_batches(
        candidates=[_step_inst()],
        evidence=JudgePromptContext(text="某执行记录"),
        call_options=JudgeCallOptions(model="glm-5.2[1m]", timeout=10),
    )
    assert seen.get("transport") == "http"


def test_judge_batch_with_retry_forwards_transport_to_call_claude(monkeypatch):
    """judge_batch_with_retry 把 transport 参转发给 call_claude(cc 默认 / http 两向)。"""
    import sift.audit.auditor as A

    seen = {}

    def fake_call_claude(prompt, config: LLMCallConfig = LLMCallConfig(), *, on_usage=None, on_progress=None):
        transport = config.transport
        seen.setdefault("transports", []).append(transport)
        return json.dumps([{"index": 0, "applicable": False}])

    monkeypatch.setattr(A, "call_claude", fake_call_claude)

    # parse 返非 unparsed 值(v[0] 非 None)→ 首次即成功、不重试(否则重试会重复记 transport)
    def parse(text, n):
        return [("ok", "")] * n

    # transport 默认 cc
    A.judge_batch_with_retry(
        parse_fn=parse,
        is_unparsed=lambda v: v[0] is None,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(model=None, timeout=10, on_progress=None, on_usage=None, schema=None, label="X"),
    )
    # 显式 http
    A.judge_batch_with_retry(
        parse_fn=parse,
        is_unparsed=lambda v: v[0] is None,
        batch=JudgeBatch(prompt="p", count=1, number=1, total=1),
        options=BatchJudgeOptions(
            model=None, timeout=10, on_progress=None, on_usage=None, schema=None, label="X", transport="http"
        ),
    )
    assert seen["transports"] == ["cc", "http"]


def _is_reference_read_step(finding):
    return finding.source == "HEADING_STEP" and finding.instruction_text == "读取 references/error-codes.md"
