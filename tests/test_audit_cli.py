# tests/test_audit_cli.py
"""audit 命令 CLI 接线测试。

mock TranscriptAuditor.audit(不真跑 LLM/判定),验证:
- 命令接线(parse_skill_md → TranscriptAuditor.audit → audit-report.json + console summary)
- --transcript 可重复(多条 transcript)
- 命令注册成功 + 现有命令非侵入仍在
- skill_path 不存在 → SkillEvalError 友好错误(照 init/run 模式)
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


def _skill_dir(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: d\n---\n# body\n",
        encoding="utf-8",
    )
    return sp


def _transcript(tmp_path: Path, name: str = "t.jsonl") -> Path:
    tp = tmp_path / name
    tp.write_text("{}\n", encoding="utf-8")
    return tp


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="myskill",
        transcripts=["t.jsonl"],
        summary={"total": 3, "pass": 1, "fail": 1, "na": 1},
        findings=[
            AuditFinding(
                instruction_text="必须做 X",
                source="CONSTRAINT",
                verdict=AuditVerdict.PASS,
                category=AuditCategory.PROHIBITION,
                evidence="未违反",
                method=AuditMethod.LLM,
                transcript="t.jsonl",
            ),
            AuditFinding(
                instruction_text="禁编造数据,违反",
                source="CONSTRAINT",
                verdict=AuditVerdict.FAIL,
                category=AuditCategory.PROHIBITION,
                evidence="执行中编造了数据",
                method=AuditMethod.LLM,
                transcript="t.jsonl",
            ),
            AuditFinding(
                instruction_text="步骤三",
                source="HEADING_STEP",
                verdict=AuditVerdict.NA,
                category=AuditCategory.STEP,
                evidence="场景不涉及该步",
                method=AuditMethod.LLM,
                transcript="t.jsonl",
            ),
        ],
        warnings=[],
    )


class _FakeAuditor:
    """记录入参,返回构造报告(不真跑 LLM/判定)。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=120, on_progress=None, refine_instructions=False):
        self.calls.append(
            {
                "skill_name": skill_name,
                "skill_body": skill_body,
                "transcripts": list(transcripts),
                "model": model,
            }
        )
        return _fake_report()


def test_audit_writes_report_and_prints_summary(monkeypatch, tmp_path):
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code == 0, r.output

    # audit-report.json 产出且是合法 AuditReport JSON
    report_file = out / "audit-report.json"
    assert report_file.exists()
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data["skill_name"] == "myskill"
    assert data["summary"] == {"total": 3, "pass": 1, "fail": 1, "na": 1}
    assert len(data["findings"]) == 3

    # console summary 含 pass/fail/na 计数
    assert "Audited" in r.output
    assert "pass=1" in r.output
    assert "fail=1" in r.output
    assert "na=1" in r.output

    # TranscriptAuditor 被正确接入:skill_name + body + transcript 透传
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["skill_name"] == "myskill"
    assert "body" in call["skill_body"]
    assert call["transcripts"] == [tp]


def test_audit_multiple_transcripts_repeatable(monkeypatch, tmp_path):
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path)
    tp1 = _transcript(tmp_path, "a.jsonl")
    tp2 = _transcript(tmp_path, "b.jsonl")
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        [
            "audit", str(sp),
            "--transcript", str(tp1),
            "--transcript", str(tp2),
            "-o", str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    assert fake.calls[0]["transcripts"] == [tp1, tp2]
    # 报告依然写出
    assert (out / "audit-report.json").exists()


def test_audit_command_registered():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "audit" in r.output


def test_existing_commands_still_present():
    """非侵入:现有命令(init/run/report/vet/...)仍注册。"""
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "run" in r.output
    assert "init" in r.output
    assert "report" in r.output


def test_audit_writes_html_alongside_json(monkeypatch, tmp_path):
    """audit 命令写完 JSON 后也写 audit-report.html(与 JSON 并列,always 写)。

    复用 _FakeAuditor 模式(不调真 LLM);HTML JS 抽出跑 node --check 验语法。
    """
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code == 0, r.output

    # 1. HTML 产出(与 JSON 并列)
    html_path = out / "audit-report.html"
    assert html_path.exists()

    # 2. JSON 没被挤掉(HTML 是追加,非替代)
    assert (out / "audit-report.json").exists()

    # 3. HTML 内 <script> JS 语法合法(node --check 退码 0)
    import re
    import subprocess
    html = html_path.read_text(encoding="utf-8")
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL))
    jsfile = tmp_path / "check.js"
    jsfile.write_text(js, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(jsfile)])
    assert result.returncode == 0, f"node --check failed:\n{result.stderr}"


def test_audit_skill_path_missing_friendly_error(tmp_path):
    """skill_path 不存在 → parse_skill_md 抛 SkillParseError → 友好错误(非裸 traceback)。"""
    missing = tmp_path / "does-not-exist"
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(missing), "--transcript", str(tp), "-o", str(out)],
    )
    # SkillParseError 是 InputError 子类(exit_code=2)→ 被 except SkillEvalError 捕获
    assert r.exit_code == 2
    # console.print 过错误信息(证明走了友好错误路径,而非裸异常)
    assert "SKILL.md" in r.output
    assert "Error" in r.output


class _OneReportAuditor:
    """返回预设 report(不真跑判定),供测 transcript_sources 渲染。"""
    def __init__(self, report):
        self._r = report

    def audit(self, *a, **k):
        return self._r


def _report_with_source(src: str = "opencode") -> AuditReport:
    """带 transcript_sources + 一个 FAIL finding(FAIL 才进 findings 表,验 transcript 列标注)。"""
    return AuditReport(
        skill_name="myskill",
        transcripts=["s.json"],
        transcript_sources=[src],
        summary={"total": 1, "pass": 0, "fail": 1, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="禁编造",
                source="CONSTRAINT",
                verdict=AuditVerdict.FAIL,
                category=AuditCategory.PROHIBITION,
                evidence="编了",
                method=AuditMethod.LLM,
                transcript="s.json",
            ),
        ],
        warnings=[],
    )


def test_audit_summary_and_findings_annotate_transcript_source(monkeypatch, tmp_path):
    """report.transcript_sources 非空 → 摘要行显 sources、findings 表 transcript 列标来源。"""
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor",
        lambda: _OneReportAuditor(_report_with_source("opencode")),
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "s.json"
    tp.write_text("{}", encoding="utf-8")
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    # 摘要行标来源(CliRunner 终端宽度可能折行,断非连续片段)
    assert "sources:" in r.output and "opencode" in r.output
    # findings 表 transcript 列带来源标注
    assert "s.json" in r.output and "opencode" in r.output


def test_audit_json_output_includes_transcript_sources(monkeypatch, tmp_path):
    """--format json 输出含 transcript_sources 字段(JSON 消费者据此知道每条 transcript 来自哪侧)。"""
    monkeypatch.setattr(
        "skill_eval.cli.commands.audit.TranscriptAuditor",
        lambda: _OneReportAuditor(_report_with_source("cc")),
    )
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text("{}", encoding="utf-8")

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "--format", "json"])
    assert r.exit_code == 0, r.output
    # JSON 含 transcript_sources 字段且值正确(避开多行 JSON 解析,直接断字符串)
    assert '"transcript_sources"' in r.output
    assert '"cc"' in r.output
