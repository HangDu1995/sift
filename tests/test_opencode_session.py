"""opencode session(JSON)→ SessionDigest + agent 切分 单测。"""
from __future__ import annotations
from pathlib import Path
import pytest

from skill_eval.evals.opencode import (
    is_opencode_session, load_opencode_session, build_digest_from_opencode,
)
from skill_eval.evals.transcript import build_digest

_FIX = Path(__file__).parent.parent / "fixtures" / "example-skills"
_LEA_OC = _FIX / "log-error-analyzer-zh" / "opencode-sessions" / "synthetic-violation.json"
_LEA_CC = _FIX / "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"


def test_is_opencode_session_by_extension_and_shape(tmp_path):
    # .json + 三键 → True;.jsonl / 缺键 / 坏 JSON → False
    assert is_opencode_session(_LEA_OC) is True
    assert is_opencode_session(_LEA_CC) is False  # .jsonl
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a session"}', encoding="utf-8")
    assert is_opencode_session(bad) is False  # 缺 session/messages/parts
    ugly = tmp_path / "ugly.json"
    ugly.write_text("{not valid", encoding="utf-8")
    assert is_opencode_session(ugly) is False  # 坏 JSON(JSONDecodeError 分支)


def test_build_digest_from_opencode_shape():
    d = build_digest_from_opencode(_LEA_OC)
    # skill_used = session.agent
    assert d.skill_used == "log-error-analyzer"
    # user_turn = prompt
    assert d.user_turns == ["分析 logs/app.log 里的错误,写 error-report.json"]
    # input_files 含 app.log,内容是 log 正文(去行号后)
    # 注:单路径 fixture 下 _detect_root 取父目录(.../logs),rel 结果是 "app.log"
    # ——与 cc build_digest 对同名 .jsonl 的输出一致(镜像 cc,不发散)。
    assert len(d.input_files) == 1
    assert d.input_files[0].path.endswith("app.log")
    assert "E-AUTH-001" in d.input_files[0].content
    # 无 write → produced_files 空(镜像 cc 没写报告)
    assert d.produced_files == []
    # tool_trace 有 read(小写)
    assert any(tc.name == "read" for tc in d.tool_trace)
    # transcript_text 含编造的 E-FAKE-999
    assert "E-FAKE-999" in d.transcript_text


def test_cc_and_opencode_digests_equivalent():
    """同场景 cc .jsonl 与 opencode .json 重建出的 digest 关键字段一致(重建等价门)。"""
    cc = build_digest(_LEA_CC)
    oc = build_digest_from_opencode(_LEA_OC)

    assert cc.user_turns == oc.user_turns
    # input_files:同 path 同内容(app.log 去行号后)
    assert [f.path for f in cc.input_files] == [f.path for f in oc.input_files]
    assert cc.input_files[0].content == oc.input_files[0].content
    # 都没写报告
    assert cc.produced_files == oc.produced_files == []
    # tool_trace 工具名归一后一致(cc Read / opencode read)
    assert [t.name.lower() for t in cc.tool_trace] == [t.name.lower() for t in oc.tool_trace]
    # transcript_text 都含编造码
    assert "E-FAKE-999" in cc.transcript_text and "E-FAKE-999" in oc.transcript_text


def test_rebuild_dispatches_opencode():
    from skill_eval.audit.rebuild import rebuild
    tool_trace, text, digest = rebuild(_LEA_OC)
    assert digest.skill_used == "log-error-analyzer"  # 走了 opencode 分支
    assert any(tc.name == "read" for tc in tool_trace)
    assert "E-FAKE-999" in text


def test_rebuild_still_handles_cc_jsonl():
    from skill_eval.audit.rebuild import rebuild
    tool_trace, text, digest = rebuild(_LEA_CC)  # cc 路径不被破坏
    assert any(tc.name.lower() == "read" for tc in tool_trace)


def test_tool_match_handles_opencode_lowercase_names():
    """opencode tool_trace 工具名小写(read),SKILL 指令工具名 Read → match_tool_call
    已大小写不敏感,应命中。不改 tool_match.py(验证而非加冗余归一)。"""
    from skill_eval.audit.tool_match import match_tool_call
    from skill_eval.models import ToolCallRecord
    trace = [ToolCallRecord(name="read", input_summary='{"filePath": "logs/app.log"}')]
    # 条件1:工具名大小写不敏感命中(Read vs read)
    assert match_tool_call("Read", "", trace) is True
    # 条件2:script_path 子串命中(opencode filePath 也含路径)
    assert match_tool_call("", "logs/app.log", trace) is True
    # 未出现的工具不命中
    assert match_tool_call("Bash", "", trace) is False


_MULTI_OC = _FIX / "multi-skill-demo" / "multi-skill.json"


def test_slice_opencode_by_agent_segments():
    from skill_eval.evals.opencode import slice_opencode_by_agent
    lea = slice_opencode_by_agent(_MULTI_OC, "log-error-analyzer")
    cra = slice_opencode_by_agent(_MULTI_OC, "code-review-auditor")
    rcz = slice_opencode_by_agent(_MULTI_OC, "release-checklist-zh")
    # LEA 2 段(run1+run4)、CRA 2 段(run2+run5)、RCZ 1 段(run3)
    assert len(lea) == 2
    assert len(cra) == 2
    assert len(rcz) == 1
    # target 未出现 → []
    assert slice_opencode_by_agent(_MULTI_OC, "nope") == []


def test_slice_opencode_purity_and_skill_used():
    """每段只含 target agent 的 message;子 session.agent = target(rebuild 取 skill_used)。

    CRA(非顶层 agent)锁 override:fixture 顶层 session.agent 是 LEA,若 slicer 没把
    子段 session.agent 改写为 target,CRA 段会继承 LEA → skill_used 错。所以必须用 CRA
    验 override,光验 LEA(顶层本就是 LEA)是 no-op 断言。
    """
    import json
    import tempfile
    from skill_eval.evals.opencode import slice_opencode_by_agent, build_digest_from_opencode

    def _skill_used_of(seg: dict) -> str:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "seg.json"
            p.write_text(json.dumps(seg, ensure_ascii=False), encoding="utf-8")
            return build_digest_from_opencode(p).skill_used

    for target in ("log-error-analyzer", "code-review-auditor"):
        segs = slice_opencode_by_agent(_MULTI_OC, target)
        assert segs  # 两 agent 都有段
        for seg in segs:
            # 纯度:段内 message 全是 target
            assert {m.get("data", {}).get("agent") for m in seg["messages"]} == {target}
            # override:子 session.agent 改写为 target(CRA 才真验)
            assert seg["session"]["agent"] == target
            # 端到端:rebuild → skill_used = target
            assert _skill_used_of(seg) == target

    # 段自带 user 子任务 prompt + read 工具(用 LEA 段验产物重建)
    lea0 = slice_opencode_by_agent(_MULTI_OC, "log-error-analyzer")[0]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "seg.json"
        p.write_text(json.dumps(lea0, ensure_ascii=False), encoding="utf-8")
        d = build_digest_from_opencode(p)
        assert d.user_turns
        assert any(tc.name == "read" for tc in d.tool_trace)
