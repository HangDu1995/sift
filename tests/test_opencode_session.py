"""opencode session(JSON)→ SessionDigest + agent 切分 单测。"""
from __future__ import annotations
import json
from pathlib import Path
import pytest

from skill_eval.evals.opencode import (
    is_opencode_session, load_opencode_session, build_digest_from_opencode,
    _records_from_opencode,
)
from skill_eval.evals.transcript import build_digest

_FIX = Path(__file__).parent.parent / "fixtures" / "example-skills"
_LEA_OC = _FIX / "log-error-analyzer-zh" / "opencode-sessions" / "synthetic-violation.json"
_LEA_CC = _FIX / "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"


def test_is_opencode_session_by_extension_and_shape(tmp_path):
    # .json + 两键(info/messages)→ True;.jsonl / 缺键 / 坏 JSON → False
    assert is_opencode_session(_LEA_OC) is True
    assert is_opencode_session(_LEA_CC) is False  # .jsonl
    bad = tmp_path / "bad.json"
    bad.write_text('{"not": "a session"}', encoding="utf-8")
    assert is_opencode_session(bad) is False  # 缺 info/messages
    ugly = tmp_path / "ugly.json"
    ugly.write_text("{not valid", encoding="utf-8")
    assert is_opencode_session(ugly) is False  # 坏 JSON(JSONDecodeError 分支)


def test_build_digest_from_opencode_shape():
    d = build_digest_from_opencode(_LEA_OC)
    # skill_used = info.agent
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
    # tool_trace 有 Read(经 _records_from_opencode 的 _norm_tool 归一为 cc 形大写,
    # 与 cc build_digest 同款;旧 inline 重建保小写,接共享重建器后统一)
    assert any(tc.name.lower() == "read" for tc in d.tool_trace)
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
    assert any(tc.name.lower() == "read" for tc in tool_trace)
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
    """每段只含 target agent 的 message;子 info.agent = target(rebuild 取 skill_used)。

    CRA(非顶层 agent)锁 override:fixture 顶层 info.agent 是 LEA,若 slicer 没把
    子段 info.agent 改写为 target,CRA 段会继承 LEA → skill_used 错。所以必须用 CRA
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
            # 纯度:段内 message.info.agent 全是 target
            assert {m.get("info", {}).get("agent") for m in seg["messages"]} == {target}
            # override:子 info.agent 改写为 target(CRA 才真验)
            assert seg["info"]["agent"] == target
            # 端到端:rebuild → skill_used = target
            assert _skill_used_of(seg) == target

    # 段自带 user 子任务 prompt + read 工具(用 LEA 段验产物重建)
    lea0 = slice_opencode_by_agent(_MULTI_OC, "log-error-analyzer")[0]
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "seg.json"
        p.write_text(json.dumps(lea0, ensure_ascii=False), encoding="utf-8")
        d = build_digest_from_opencode(p)
        assert d.user_turns
        assert any(tc.name.lower() == "read" for tc in d.tool_trace)


# 真 `opencode export` 产出的会话(本地 /tmp/oc_export.json,缺席则 skip)。
# 证明 build_digest_from_opencode 吃的是真实生产格式,非自造。生成:
#   opencode session list  # 找 sessionID
#   opencode export <sessionID> > /tmp/oc_export.json
_REAL_EXPORT = Path("/tmp/oc_export.json")


def test_real_opencode_export_consumed():
    """真 opencode export 输出能被 build_digest_from_opencode 消费(非自造格式证明)。"""
    if not _REAL_EXPORT.exists():
        import pytest
        pytest.skip(f"无真 export fixture:({_REAL_EXPORT};生成见上注释)")
    assert is_opencode_session(_REAL_EXPORT) is True
    d = build_digest_from_opencode(_REAL_EXPORT)
    # 真 session 有 tool_trace(该 session 跑了 read/bash/write 等)或至少有内容
    assert d.tool_trace or d.transcript_text or d.user_turns
    assert d.skill_used  # info.agent 非空


# 真 opencode .db 端到端:audit --db 路径的 list→export→digest 闭环(需 opencode CLI + 真 .db)。
# 生成:cp <你的 opencode.db> /tmp/oc_export.db
#   (opencode db 默认在 ~/.local/share/opencode/opencode.db,或 $XDG_DATA_HOME/opencode/ 下)
_REAL_DB = Path("/tmp/oc_export.db")


def test_real_opencode_db_export_consumed():
    """真 .db → list_sessions/export_session → build_digest_from_opencode 端到端通(audit --db 闭环证明)。"""
    import shutil
    import tempfile
    import pytest
    if not _REAL_DB.exists():
        pytest.skip(f"无真 .db fixture:({_REAL_DB};生成见上注释)")
    if not shutil.which("opencode"):
        pytest.skip("opencode CLI 未装")
    from skill_eval.evals.opencode_db import (
        export_session, list_sessions, resolve_session,
    )

    sessions = list_sessions(_REAL_DB)
    if not sessions:
        pytest.skip("真 .db 内无 session")
    # 取最新那条(list_sessions 已按 updated desc 排)
    target = resolve_session(sessions, sessions[0]["id"])
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write(export_session(_REAL_DB, target["id"]))
        p = Path(f.name)
    try:
        assert is_opencode_session(p) is True
        d = build_digest_from_opencode(p)
        assert d.tool_trace or d.transcript_text or d.user_turns
    finally:
        p.unlink(missing_ok=True)


def _oc_session(parts_user, parts_asst):
    """造最小 opencode export 形 session JSON。"""
    return {"info": {"agent": "log-error-analyzer", "time": {"created": 1}},
            "messages": [{"info": {"role": "user"}, "parts": parts_user},
                         {"info": {"role": "assistant"}, "parts": parts_asst}]}


def test_records_from_opencode_basic_shape(tmp_path):
    sess = _oc_session(
        [{"type": "text", "text": "do it"}],
        [{"type": "text", "text": "thinking"},
         {"type": "tool", "tool": "read",
          "state": {"input": {"filePath": "/p/a.py"}, "output": "1  hello\n2  world"}}])
    p = tmp_path / "s.json"; p.write_text(json.dumps(sess))
    recs = _records_from_opencode(p)
    # 形:recs[0]=user prompt(text 保留,喂 _digest_from_records 抽 user_turns,
    #     与 cc 路径经 _records_from_cc 保留 user 消息对齐→digest 等价),
    #     recs[1]=assistant(text+tool_use), recs[2]=user(tool_result)
    assert recs[0]["message"]["role"] == "user"
    assert recs[1]["message"]["role"] == "assistant"
    types = [b["type"] for b in recs[1]["message"]["content"]]
    assert types == ["text", "tool_use"]
    tu = next(b for b in recs[1]["message"]["content"] if b["type"] == "tool_use")
    assert tu["name"] == "Read"                       # 小写归大写
    assert tu["input"]["file_path"] == "/p/a.py"      # filePath → file_path
    assert recs[2]["message"]["role"] == "user"
    tr = next(b for b in recs[2]["message"]["content"] if b["type"] == "tool_result")
    assert "hello" in tr["content"]


def test_records_from_opencode_edit_field_mapping(tmp_path):
    sess = _oc_session([{"type": "text", "text": "go"}],
        [{"type": "tool", "tool": "edit",
          "state": {"input": {"filePath": "/p/a.py", "oldString": "x", "newString": "y", "replaceAll": True},
                    "output": "ok"}}])
    p = tmp_path / "e.json"; p.write_text(json.dumps(sess))
    recs = _records_from_opencode(p)
    # recs[0]=user prompt; recs[1]=assistant(tool_use)
    asst = next(r for r in recs if r["message"]["role"] == "assistant")
    tu = next(b for b in asst["message"]["content"] if b["type"] == "tool_use")
    assert tu["name"] == "Edit"
    assert tu["input"] == {"file_path": "/p/a.py", "old_string": "x", "new_string": "y", "replace_all": True}


def test_opencode_edit_reconstructs(tmp_path):
    """read→edit 命中 → 经 canonical 重建器产出 edit 后内容(opencode Task 3-4 落地能力验证)。

    旧 opencode 路径只有 read/write/bash,edit 不重建;接共享重建器后应自动获得。
    本测端到端验证:read 拿到 base → edit 把 beta→BETA → produced_files 含 BETA。
    """
    base = "1  alpha\n2  beta\n"
    sess = {"info": {"agent": "x", "time": {"created": 1}}, "messages": [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "edit it"}]},
        {"info": {"role": "assistant"}, "parts": [
            {"type": "text", "text": "ok"},
            {"type": "tool", "tool": "read",
             "state": {"input": {"filePath": "/p/a.py"}, "output": base}},
            {"type": "tool", "tool": "edit",
             "state": {"input": {"filePath": "/p/a.py", "oldString": "beta", "newString": "BETA"},
                       "output": "ok"}}]}]}
    p = tmp_path / "e.json"; p.write_text(json.dumps(sess))
    d = build_digest_from_opencode(p)
    # edit 命中 base → produced_files 含 edit 后内容
    assert any("BETA" in f.content for f in d.produced_files)
    # 不应残留未 edit 的 base 副本(有 beta\n 且无 BETA 的 produced_file 即残留)
    assert not any("beta\n" in f.content and "BETA" not in f.content for f in d.produced_files)


def test_opencode_edit_missing_old_demotes(tmp_path):
    """edit oldString 在 read base 里找不到 → demoted → 进 unreconstructed(此前 opencode 无此能力)。

    验证共享重建器的 demote 推理在 opencode 路径上端到端工作。
    """
    sess = {"info": {"agent": "x", "time": {"created": 1}}, "messages": [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "edit"}]},
        {"info": {"role": "assistant"}, "parts": [
            {"type": "tool", "tool": "read",
             "state": {"input": {"filePath": "/p/a.py"}, "output": "1  alpha\n"}},
            {"type": "tool", "tool": "edit",
             "state": {"input": {"filePath": "/p/a.py", "oldString": "GHOST", "newString": "x"},
                       "output": "ok"}}]}]}
    p = tmp_path / "g.json"; p.write_text(json.dumps(sess))
    d = build_digest_from_opencode(p)
    # oldString 不在 base → demoted → 进 unreconstructed
    assert any("a.py" in u for u in d.unreconstructed)
