# tests/test_opencode_cli.py
"""audit CLI --kind skill 对 opencode session 的分派 + 子段写盘格式。

--kind skill 分支必须按 transcript 格式分派:
- opencode(.json)→ slice_opencode_by_agent 切段 + 写子 .json
- cc(.jsonl)→ slice_transcript + 写子 .jsonl(原逻辑不变)

子段透传 TranscriptAuditor(rebuild 再按各子段格式分派)。fake auditor 不真跑判定,
只记录收到的 transcripts 列表 + 返回合法 AuditReport。
"""
from __future__ import annotations

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

_FIX = Path(__file__).parent.parent / "fixtures" / "example-skills"
runner = CliRunner()


def _fake_report(skill_name: str = "log-error-analyzer") -> AuditReport:
    """合法 AuditReport:findings=[],summary 全 0(满足 audit.py 下游对
    summary/by_instruction/findings 的访问)。"""
    return AuditReport(
        skill_name=skill_name,
        transcripts=[],
        summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
        findings=[],
        warnings=[],
    )


class _FakeAuditor:
    """记录入参(尤其 transcripts 列表),返回合法 AuditReport(不真跑 LLM/判定)。

    签名对齐 test_audit_by_skill.py 的 _FakeAuditor 与 audit.py 的真实调用。
    """

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None,
              timeout=600, on_progress=None, refine_instructions=False, generate_headline=False, locate_turns=False):
        self.calls.append({
            "skill_name": skill_name,
            "transcripts": list(transcripts),
        })
        return _fake_report(skill_name)


def test_audit_byskill_opencode_writes_json_slices(monkeypatch, tmp_path):
    """opencode fixture --kind skill:切出的子段是 .json,且仍是合法 opencode session,透传 auditor。"""
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    skill = _FIX / "log-error-analyzer"
    multi = _FIX / "multi-skill-demo" / "multi-skill.json"
    out = tmp_path / "out"

    res = runner.invoke(
        app,
        ["audit", str(skill), "--transcript", str(multi),
         "--kind", "skill", "-o", str(out)],
    )
    assert res.exit_code == 0, res.output

    assert len(fake.calls) == 1
    sliced = fake.calls[0]["transcripts"]
    # 切出非空 + 全 .json 后缀 + 路径都在 slices/ 下
    assert sliced, "opencode 段为空(应切出 log-error-analyzer 的段)"
    assert all(Path(p).suffix == ".json" for p in sliced), sliced
    assert all("slices" in str(p) for p in sliced), sliced
    # 子段仍是合法 opencode session(rebuild 能按格式分派)
    from skill_eval.evals.opencode import is_opencode_session
    assert all(is_opencode_session(Path(p)) for p in sliced), sliced


def test_audit_byskill_cc_still_writes_jsonl(monkeypatch, tmp_path):
    """cc 路径不被破坏:--kind skill 仍写 .jsonl 子段。"""
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    skill = _FIX / "log-error-analyzer"
    multi_cc = _FIX / "multi-skill-demo" / "multi-skill.jsonl"
    out = tmp_path / "out"

    res = runner.invoke(
        app,
        ["audit", str(skill), "--transcript", str(multi_cc),
         "--kind", "skill", "-o", str(out)],
    )
    assert res.exit_code == 0, res.output

    assert len(fake.calls) == 1
    sliced = fake.calls[0]["transcripts"]
    assert sliced, "cc 段为空(应切出 log-error-analyzer 的段)"
    assert all(Path(p).suffix == ".jsonl" for p in sliced), sliced
