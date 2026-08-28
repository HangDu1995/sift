# tests/test_audit_cli_turn_refs.py
"""audit 命令 --turn-refs/--no-turn-refs CLI 开关测试。

locate_turns(Task 1+2 已让 rebuild / judge 默认关 turn 定位)在 Task 3 从 CLI Option
贯穿到 audit() → rebuild + judge。本测不真跑 LLM/判定:mock TranscriptAuditor 记录
audit() 收到的 locate_turns,断 CLI 默认(--no-turn-refs → False)与显式开(--turn-refs → True)。

照 tests/test_audit_max_concurrent.py 的 _FakeAuditor + monkeypatch 模式。
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sift.cli.main import app
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)

runner = CliRunner()


def _skill_dir(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: d\n---\n# body\n",
        encoding="utf-8",
    )
    return sp


def _transcript(tmp_path: Path) -> Path:
    tp = tmp_path / "t.jsonl"
    # rebuild 会被 mock 掉,无需真实记录;单行合法 JSONL 即可让 CLI 接线跑到 audit() 调用。
    tp.write_text(json.dumps({"message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "hi"}]}}),
                  encoding="utf-8")
    return tp


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="myskill",
        transcripts=["t.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[AuditFinding(
            instruction_text="do X", source="HEADING_STEP",
            verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
            evidence="ok", method=AuditMethod.LLM, transcript="t.jsonl",
        )],
        warnings=[],
    )


class _FakeAuditor:
    """记录 audit() 入参(含 locate_turns),返回预设报告(不真跑 LLM/判定)。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600,
              on_progress=None, refine_instructions=False, generate_headline=False, locate_turns=False, declared_tools=None):
        self.calls.append({"locate_turns": locate_turns})
        return _fake_report()


def _setup(monkeypatch):
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)
    return fake


def test_audit_default_no_turn_refs(monkeypatch, tmp_path):
    """不带 --turn-refs → audit() 收到 locate_turns=False(默认关 turn 定位)。"""
    fake = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code == 0, r.output
    assert len(fake.calls) == 1
    assert fake.calls[0]["locate_turns"] is False


def test_audit_turn_refs_flag_passes_true(monkeypatch, tmp_path):
    """--turn-refs → audit() 收到 locate_turns=True(开 turn 定位 + digest [T{idx}] 前缀)。"""
    fake = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app, ["audit", str(sp), "--transcript", str(tp),
              "--turn-refs", "-o", str(out)],
    )
    assert r.exit_code == 0, r.output
    assert len(fake.calls) == 1
    assert fake.calls[0]["locate_turns"] is True
