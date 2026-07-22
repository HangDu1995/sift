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
