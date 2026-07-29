"""rebuild 的 locate_turns 开关:[T{idx}] 前缀 + ToolCallRecord.turn 的渲染控制。"""
from pathlib import Path
from skill_eval.audit.rebuild import rebuild


def _write_cc_jsonl(tmp_path: Path) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"},"parent_tool_use_id":null}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"do X"}]}}\n',
        encoding="utf-8",
    )
    return p


def test_locate_turns_off_no_prefix(tmp_path):
    tp = _write_cc_jsonl(tmp_path)
    _trace, text, digest = rebuild(tp, locate_turns=False)
    assert "[T" not in text
    assert all(t.turn is None for t in digest.tool_trace)


def test_locate_turns_on_has_prefix(tmp_path):
    tp = _write_cc_jsonl(tmp_path)
    _trace, text, _digest = rebuild(tp, locate_turns=True)
    # user record 占 turn 1,assistant 占 turn 2 → 前缀 [T2](stamp 按全体 record 位置)。
    assert "[T2]" in text


def test_locate_turns_default_off(tmp_path):
    tp = _write_cc_jsonl(tmp_path)
    _trace, text, _digest = rebuild(tp)
    assert "[T" not in text
