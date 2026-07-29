# tests/test_audit_root.py
"""audit --kind root CLI 接线测试(cc + opencode 双平台)。

照 test_audit_by_skill.py 模式:mock TranscriptAuditor(不真跑 LLM),验证:
- cc --kind root:主作用域(parent_tool_use_id 为空)切给 auditor,排除子 agent 内部
- opencode --kind root:以 session.info.agent 为 target(复用 slice_opencode_by_agent)
- target 名忽略(root 不按声明名切,声明名 "wf" ≠ 主 agent)
- opencode session 缺 info.agent → exit 2
"""
from __future__ import annotations
import json
from pathlib import Path
from typer.testing import CliRunner
from skill_eval.cli.main import app
from skill_eval.models.audit_models import (
    AuditCategory, AuditFinding, AuditMethod, AuditReport, AuditVerdict,
)

runner = CliRunner()


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _asst(t): return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}
def _tool_use(tid, name, inp):
    return {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}
def _agent(tid): return {"message": {"role": "assistant", "content": [
    {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "x"}}]}}
def _tool_result(tid, text): return {"message": {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": tid, "content": text}]}}
def _in_sub(parent, rec):
    rec["parent_tool_use_id"] = parent
    return rec


def _skill_dir(tmp_path: Path, name: str = "wf") -> Path:
    sp = tmp_path / name
    sp.mkdir()
    (sp / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n# body\n", encoding="utf-8")
    return sp


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="wf", transcripts=["slice.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[AuditFinding(instruction_text="orchestrate", source="HEADING_STEP",
                               verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                               evidence="ok", method=AuditMethod.LLM, transcript="slice.jsonl")],
        warnings=[],
    )


class _FakeAuditor:
    def __init__(self): self.calls: list[dict] = []
    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600,
              on_progress=None, refine_instructions=False, locate_turns=False):
        self.calls.append({"skill_name": skill_name, "transcripts": list(transcripts)})
        return _fake_report()


def test_kind_root_cc_slices_main_scope(monkeypatch, tmp_path):
    """cc:--kind root 把主作用域切给 auditor,排除子 agent 内部;声明名忽略。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path, "wf")      # 声明名 "wf" ≠ 主 agent(root 忽略名)
    a_id = "call_A"
    records = [
        _asst("MAIN_TXT"),
        _tool_use("r1", "Read", {"file_path": "/tmp/app.log"}),
        _agent(a_id),                    # 主发起 dispatch(主作用域)
        _in_sub(a_id, _asst("SUB_TXT")),  # 子 agent 内部(排除)
        _tool_result(a_id, "ok"),         # 主收 dispatch 返回(主作用域)
    ]
    tp = _write_jsonl(tmp_path / "s.jsonl", records)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp),
                            "--kind", "root", "-o", str(out)])
    assert r.exit_code == 0, r.output

    assert len(fake.calls) == 1
    slices = fake.calls[0]["transcripts"]
    assert len(slices) == 1                          # 主作用域单 span → 1 切片
    content = Path(slices[0]).read_text(encoding="utf-8")
    assert "MAIN_TXT" in content and "/tmp/app.log" in content
    assert "SUB_TXT" not in content                  # 子 agent 内部排除


def test_kind_root_opencode_uses_session_info_agent(monkeypatch, tmp_path):
    """opencode:--kind root 以 session.info.agent 为 target(复用 slice_opencode_by_agent)。"""
    from skill_eval.evals.opencode import load_opencode_session
    fake = _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    p = Path("fixtures/example-skills/multi-skill-demo/multi-skill.json")
    main_agent = (load_opencode_session(p).get("info") or {}).get("agent")
    assert main_agent

    sp = _skill_dir(tmp_path, "wf")                # 声明名 "wf" ≠ main_agent(root 忽略名)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(p),
                            "--kind", "root", "-o", str(out)])
    assert r.exit_code == 0, r.output

    slices = fake.calls[0]["transcripts"]
    assert slices                                  # 主 agent 段非空
    for seg_path in slices:
        seg = json.loads(Path(seg_path).read_text(encoding="utf-8"))
        assert (seg.get("info") or {}).get("agent") == main_agent


def test_kind_root_opencode_missing_info_agent_exits_2(monkeypatch, tmp_path):
    """opencode session 缺 info.agent → --kind root exit 2(无法定主 agent,不喂 auditor)。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"info": {}, "messages": []}), encoding="utf-8")
    sp = _skill_dir(tmp_path, "wf")
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(bad),
                            "--kind", "root", "-o", str(out)])
    assert r.exit_code == 2
    assert "info.agent" in r.output
    assert len(fake.calls) == 0                      # 没喂 auditor(提前退出)
