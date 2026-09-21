"""rebuild 后 transcript_text/tool_trace 带 [T{idx}] 标识;切片后保留全局 seq(不重编号)。"""

import json

from sift.audit.rebuild import rebuild
from sift.audit.slice import slice_transcript


def _write_jsonl(p, recs):
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs))


def _asst(t):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _user(t):
    return {"message": {"role": "user", "content": t}}


def _agent(tid):
    return {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "x"}}],
        }
    }


def _in_sub(parent, rec):
    rec["parent_tool_use_id"] = parent
    return rec


def test_transcript_text_carries_turn_prefix(tmp_path):
    p = tmp_path / "s.jsonl"
    # 末尾补一条 user turn 让 rebuild 的空 digest 守卫通过(user_turns 非空);
    # assistant record 仍在 turn 1/2,断言语义不变。
    _write_jsonl(p, [_asst("FIRST"), _asst("SECOND"), _user("ok")])
    _tool_trace, transcript_text, _digest = rebuild(p, locate_turns=True)
    assert "[T1]" in transcript_text and "FIRST" in transcript_text
    assert "[T2]" in transcript_text and "SECOND" in transcript_text


def test_tool_trace_carries_turn(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/x"}}],
                }
            },
        ],
    )
    tool_trace, _tt, _d = rebuild(p, locate_turns=True)
    assert tool_trace[0].turn == 1


def test_root_slice_preserves_global_turn_index(tmp_path):
    """切片(--kind root)后 transcript_text 的 seq 是原全局位置,不重编号。"""
    p = tmp_path / "s.jsonl"
    a_id = "call_A"
    _write_jsonl(
        p,
        [
            _asst("MAIN1"),  # turn 1(主作用域)
            _agent(a_id),  # turn 2(主作用域,dispatch)
            _in_sub(a_id, _asst("SUB1")),  # turn 3(子 agent 内部,被 root 排除)
            _asst("MAIN2"),  # turn 4(主作用域)
        ],
    )
    segs = slice_transcript(p, "wf", "root")
    assert len(segs) == 1
    seg_path = tmp_path / "seg.jsonl"
    _write_jsonl(seg_path, segs[0])
    _tt, transcript_text, _d = rebuild(seg_path, locate_turns=True)
    # root span 含 turn 1/2/4(排除 turn 3 子 agent);seq 是原全局,非段内 1/2/3
    assert "[T1]" in transcript_text and "MAIN1" in transcript_text
    assert "[T2]" in transcript_text  # dispatch 在 turn 2
    assert "[T4]" in transcript_text and "MAIN2" in transcript_text
    assert "[T3]" not in transcript_text  # 子 agent 内部被排除
