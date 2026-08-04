# tests/test_audit_db_cli.py
"""audit --db / --session CLI 接线测试。

mock list_sessions / export_session / TranscriptAuditor(不真调 opencode、不真跑 LLM),
验:--db 解析→export→喂 auditor、互斥校验、多 session 列清单 exit 2、单 session 自动选、清理临时 json。
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from skill_eval.cli.main import app
from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)

runner = CliRunner()

_EXPORT = json.dumps({"info": {"id": "ses_x", "agent": "build"}, "messages": []})


def _skill_dir(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: d\n---\n# body\n",
        encoding="utf-8",
    )
    return sp


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="myskill",
        transcripts=["ses_x.json"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="做 X",
                source="CONSTRAINT",
                verdict=AuditVerdict.PASS,
                category=AuditCategory.PROHIBITION,
                evidence="ok",
                method=AuditMethod.LLM,
                transcript="ses_x.json",
            ),
        ],
        warnings=[],
    )


class _FakeAuditor:
    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=120,
              on_progress=None, refine_instructions=False, generate_headline=False, locate_turns=False):
        self.calls.append({"transcripts": list(transcripts)})
        return _fake_report()


# patch 三个 audit 模块级名字(模块级 import 使其可被 monkeypatch 替换,同 TranscriptAuditor)
def _patch_db(monkeypatch, *, list_ret, export=_EXPORT, audit=None):
    monkeypatch.setattr("skill_eval.cli.commands.audit.list_sessions", lambda db: list_ret)
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.export_session", lambda db, sid: export
    )
    fake = audit if audit is not None else _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)
    return fake


def test_audit_db_with_session_exports_and_audits(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    fake = _patch_db(monkeypatch, list_ret=[{"id": "ses_x", "title": "T", "updated": 0}])

    r = runner.invoke(app, ["audit", str(sp), "--db", str(db), "--session", "ses_x", "-o", str(out)])
    assert r.exit_code == 0, r.output

    # auditor 收到的是 export 出的临时 .json(单个)
    assert len(fake.calls) == 1
    used = fake.calls[0]["transcripts"][0]
    assert used.suffix == ".json"
    # 报告写出
    assert (out / "audit-report.json").exists()
    # 临时 json 已被 finally 清理
    assert not used.exists()


def test_audit_db_and_transcript_mutually_exclusive(tmp_path):
    sp = _skill_dir(tmp_path)
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["audit", str(sp), "--db", str(db), "--transcript", str(tp)])
    assert r.exit_code == 2
    assert "互斥" in r.output


def test_audit_session_without_db_errors(tmp_path):
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["audit", str(sp), "--session", "ses_x"])
    assert r.exit_code == 2
    assert "--db" in r.output


def test_audit_neither_db_nor_transcript_errors(tmp_path):
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["audit", str(sp)])
    assert r.exit_code == 2
    assert "--transcript" in r.output and "--db" in r.output


def test_audit_db_multi_sessions_without_session_lists_and_exits2(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    sp = _skill_dir(tmp_path)
    _patch_db(
        monkeypatch,
        list_ret=[
            {"id": "ses_a", "title": "甲", "updated": 200},
            {"id": "ses_b", "title": "乙", "updated": 100},
        ],
    )
    r = runner.invoke(app, ["audit", str(sp), "--db", str(db)])
    assert r.exit_code == 2
    # 列出两个 session id,提示带 --session 重跑
    assert "ses_a" in r.output and "ses_b" in r.output
    assert "--session" in r.output


def test_audit_db_single_session_autopicks(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    exported = {}

    def fake_export(d, sid):
        exported["sid"] = sid
        return _EXPORT

    monkeypatch.setattr("skill_eval.cli.commands.audit.list_sessions",
                        lambda db: [{"id": "ses_only", "title": "唯一", "updated": 0}])
    monkeypatch.setattr("skill_eval.cli.commands.audit.export_session", fake_export)
    fake = _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    r = runner.invoke(app, ["audit", str(sp), "--db", str(db), "-o", str(out)])
    assert r.exit_code == 0, r.output
    # 自动选了唯一 session(无需 --session)
    assert exported["sid"] == "ses_only"
    assert len(fake.calls) == 1
