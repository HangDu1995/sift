"""tests for sift.audit.rebuild.

rebuild 是 build_digest 的薄封装:加错误统一 + 三元组解包。
合成 JSONL,非 mock。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sift.audit.rebuild import rebuild
from sift.errors import SiftError
from sift.evals.transcript import SessionDigest
from sift.models import ToolCallRecord


def _assistant_text(text: str) -> dict:
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_use(name: str, tid: str, inp: dict) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": inp, "id": tid}],
        }
    }


def _tool_result(tid: str, content: str) -> dict:
    return {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}],
        }
    }


def _user_text(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def test_rebuild_normal_returns_tuple(tmp_path: Path) -> None:
    """合成正常 JSONL(含 tool_use + text)→ 三元组,各字段非空、类型正确。"""
    p = _write_jsonl(
        tmp_path / "session.jsonl",
        [
            _user_text("请读取文件"),
            _assistant_text("我现在读取它"),
            _tool_use("Read", "t1", {"file_path": "/tmp/x.txt"}),
            _tool_result("t1", "1\t内容"),
        ],
    )

    tool_trace, text, digest = rebuild(p, locate_turns=True)

    assert isinstance(tool_trace, list)
    assert len(tool_trace) == 1
    assert isinstance(tool_trace[0], ToolCallRecord)
    assert tool_trace[0].name == "Read"
    assert text == "[T2] 我现在读取它\n[T3]"
    assert isinstance(digest, SessionDigest)
    # rebuild 是解包同一 digest:返回的对象即 digest 的字段(同一引用)
    assert digest.tool_trace is tool_trace
    assert digest.transcript_text == text


def test_rebuild_missing_file_raises(tmp_path: Path) -> None:
    """文件不存在 → SiftError,消息含路径。"""
    missing = tmp_path / "nope.jsonl"
    with pytest.raises(SiftError) as exc:
        rebuild(missing)
    assert str(missing) in str(exc.value)


def test_rebuild_empty_file_raises(tmp_path: Path) -> None:
    """完全空文件 → digest 全空 → SiftError(消息含"无可对账内容")。"""
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")

    with pytest.raises(SiftError) as exc:
        rebuild(p)
    assert "无可对账内容" in str(exc.value)


def test_rebuild_whitespace_only_file_raises(tmp_path: Path) -> None:
    """只有空行/空白(digest 全空)→ SiftError。"""
    p = tmp_path / "ws.jsonl"
    p.write_text("   \n\n  \n", encoding="utf-8")

    with pytest.raises(SiftError) as exc:
        rebuild(p)
    assert "无可对账内容" in str(exc.value)


def test_rebuild_bad_lines_skipped_when_good_lines_present(tmp_path: Path) -> None:
    """坏 JSONL 行由 build_digest 跳过,但有合法行 → 正常返回(不抛)。"""
    p = tmp_path / "mixed.jsonl"
    raw_lines = [
        "{this is not valid json",  # 坏行 → 跳过
        json.dumps(_user_text("做点什么"), ensure_ascii=False),  # 合法 user turn
        "   ",  # 空白行 → 跳过
        json.dumps(_tool_use("Read", "t1", {"file_path": "/tmp/y.txt"}), ensure_ascii=False),
        "}",  # 坏行 → 跳过
        json.dumps(_tool_result("t1", "内容"), ensure_ascii=False),
    ]
    p.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

    tool_trace, text, digest = rebuild(p)

    # 只有一根合法 tool_use,坏行未产生额外条目
    assert len(tool_trace) == 1
    assert tool_trace[0].name == "Read"
    assert isinstance(digest, SessionDigest)
    assert digest.user_turns == ["做点什么"]
