# tests/test_audit_structured.py
"""audit --transcript 指向结构化 records JSON 的接线测试。

mock TranscriptAuditor(不真跑 LLM),验证:
- 结构化输入被展开成临时 cc-jsonl 喂给 auditor(不是原 .json);
- 展开内容是 cc 记录形(skill 归一成 Skill / input.skill),非原 {format:...} 结构;
- --kind skill 在展开后的记录上按 skill 切段(producer 不必手拼 cc-jsonl)。
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


def _skill_dir(tmp_path: Path, name: str = "skillA") -> Path:
    sp = tmp_path / name
    sp.mkdir()
    (sp / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n# body\n", encoding="utf-8")
    return sp


def _structured(path: Path, records: list[dict]) -> Path:
    path.write_text(
        json.dumps({"format": "skill-eval-records", "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _turn(tool_calls: list[dict], role: str = "assistant", text: str | None = None) -> dict:
    rec: dict = {"role": role, "tool_calls": tool_calls}
    if text:
        rec["text"] = text
    return rec


def _skill_call(name: str) -> dict:
    return {"name": "skill", "input": {"name": name}, "result": f"loaded {name}"}


def _read_call(fp: str) -> dict:
    return {"name": "read", "input": {"file_path": fp}, "result": "content"}


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="skillA", transcripts=["x.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[AuditFinding(instruction_text="do", source="STEP",
                               verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                               evidence="e", method=AuditMethod.LLM, transcript="x.jsonl")],
        warnings=[],
    )


class _CapturingAuditor:
    """记录 auditor 收到的 transcript 路径 + 调用时的文件内容(finally 清临时文件前抓)。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600,
              on_progress=None, refine_instructions=False):
        captured = []
        for tp in transcripts:
            try:
                content = Path(tp).read_text(encoding="utf-8")
            except OSError:
                content = ""
            captured.append({"path": str(tp), "content": content})
        self.calls.append({"skill_name": skill_name, "transcripts": captured})
        return _fake_report()


def test_structured_input_expanded_to_cc_jsonl(monkeypatch, tmp_path):
    """无 --kind:结构化 JSON → 展开成临时 cc-jsonl 喂 auditor(非原 .json),
    内容是 cc 记录形(skill→Skill / input.skill),不再是 {format:...} 原结构。"""
    fake = _CapturingAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path, "skillA")
    tp = _structured(tmp_path / "s.json", [
        _turn([_skill_call("skillA"), _read_call("/tmp/A.md")], text="go"),
    ])
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output

    assert len(fake.calls) == 1
    ts = fake.calls[0]["transcripts"]
    assert len(ts) == 1
    # 喂的是展开后的 cc-jsonl,不是原结构化 .json
    assert ts[0]["path"].endswith(".jsonl")
    assert ts[0]["path"] != str(tp)
    # 内容是 cc 记录形:skill 归一成 Skill + input.skill;不再是 skill-eval-records 原结构
    c = ts[0]["content"]
    assert '"Skill"' in c
    assert '"skill": "skillA"' in c
    assert "skill-eval-records" not in c


def test_structured_input_with_by_skill_slices(monkeypatch, tmp_path):
    """--kind skill:在展开后的记录上按 target 切段 —— producer 不必手拼 cc-jsonl 也能多 skill 切分。

    输入 skillA(+read A)、skillB(+read B);对 skillA 切:只含 A 的段、不含 B / skillB。
    """
    fake = _CapturingAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path, "skillA")
    tp = _structured(tmp_path / "s.json", [
        _turn([_skill_call("skillA")]),
        _turn([_read_call("/tmp/A.md")]),
        _turn([_skill_call("skillB")]),
        _turn([_read_call("/tmp/B.md")]),
    ])
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "--kind", "skill", "-o", str(out)])
    assert r.exit_code == 0, r.output

    ts = fake.calls[0]["transcripts"]
    assert len(ts) == 1  # skillA 被调 1 次 → 1 段
    joined = ts[0]["content"]
    assert "/tmp/A.md" in joined
    assert "/tmp/B.md" not in joined
    assert "skillB" not in joined
