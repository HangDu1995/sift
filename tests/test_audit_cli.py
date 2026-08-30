# tests/test_audit_cli.py
"""audit 命令 CLI 接线测试。

mock TranscriptAuditor.audit(不真跑 LLM/判定),验证:
- 命令接线(parse_skill_md → TranscriptAuditor.audit → audit-report.json + console summary)
- --transcript 可重复(多条 transcript)
- 命令注册成功 + 现有命令非侵入仍在
- skill_path 不存在 → SiftError 友好错误(照 init/run 模式)
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


def _transcript(tmp_path: Path, name: str = "t.jsonl") -> Path:
    tp = tmp_path / name
    tp.write_text("{}\n", encoding="utf-8")
    return tp


def _fake_report() -> AuditReport:
    from sift.models.audit_models import BoundarySummary, CoverageSummary, ToolUsageEntry

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
        coverage=CoverageSummary(total=3, covered=1, uncovered=2, unknown=0, coverage_rate=1 / 3),
        boundary=BoundarySummary(
            total_tools=2, declared=1, undeclared=1,
            tools=[
                ToolUsageEntry(name="mcp__db__query", classification="undeclared-domain",
                               sessions=1, calls=2, transcripts=["t.jsonl"]),
                ToolUsageEntry(name="mcp__lint__check", classification="domain",
                               declared_by="用 mcp__lint__check", sessions=1, calls=1,
                               transcripts=["t.jsonl"]),
            ],
        ),
    )


class _FakeAuditor:
    """记录入参,返回构造报告(不真跑 LLM/判定)。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=120, on_progress=None, refine_instructions=False, generate_headline=False, locate_turns=False, declared_tools=None):
        self.calls.append(
            {
                "skill_name": skill_name,
                "skill_body": skill_body,
                "transcripts": list(transcripts),
                "model": model,
                "declared_tools": declared_tools,
            }
        )
        return _fake_report()


def test_audit_sift_error_prints_triage(monkeypatch, tmp_path):
    """audit 撞 SiftError(命令级)→ except 里 classify 打印根因+建议。"""
    from sift.errors import SiftError

    class _BoomAuditor:
        def audit(self, *a, **kw):
            raise SiftError("opencode session export 超时(timeout 60s)")

    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: _BoomAuditor()
    )

    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code != 0  # SiftError → 非零退出
    assert "超时" in r.output or "timeout" in r.output  # 原始消息保留
    # triage 建议(timeout 类 → --timeout/--max-turns)
    assert "--timeout" in r.output or "--max-turns" in r.output


def test_audit_writes_report_and_prints_summary(monkeypatch, tmp_path):
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
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

    # 声明覆盖一行(covered/total + 未覆盖/未知);coverage 也落盘进 JSON
    assert "声明覆盖" in r.output
    assert "1/3" in r.output
    assert "未覆盖 2" in r.output
    assert data["coverage"]["covered"] == 1
    assert data["coverage"]["uncovered"] == 2

    # 工具对账一行(越界视图一期,信息性);boundary 也落盘进 JSON
    assert "工具对账" in r.output
    assert "未声明领域工具 1 个" in r.output
    assert "不构成违规判定" in r.output
    assert data["boundary"]["undeclared"] == 1
    assert data["boundary"]["tools"][0]["name"] == "mcp__db__query"

    # TranscriptAuditor 被正确接入:skill_name + body + transcript 透传
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["skill_name"] == "myskill"
    assert "body" in call["skill_body"]
    assert call["transcripts"] == [tp]


def test_audit_boundary_line_hidden_when_no_undeclared(monkeypatch, tmp_path):
    """undeclared=0(全 builtin / 全声明命中)→ 不打工具对账行(非零才打)。"""
    report = _fake_report()
    report.boundary.undeclared = 0
    fake = _FakeAuditor()
    fake.audit = lambda *a, **kw: report
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "工具对账" not in r.output


def test_audit_findings_table_marks_quantity_findings(monkeypatch, tmp_path):
    """control_flow='quantity' 的非 PASS finding 在 findings 表 instruction 前加 (数量) 记号
    ——数量 finding 沿用原指令原文/原类别(对聚合透明),不加记号在报告里认不出是数量判定
    (实测连作者都要翻 evidence 找);普通 FAIL 不加。"""
    report = _fake_report()
    qf = next(f for f in report.findings if f.verdict == AuditVerdict.FAIL)
    qf.control_flow = "quantity"
    fake = _FakeAuditor()
    fake.audit = lambda *a, **kw: report
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert "(数量)" in r.output
    # 落盘 JSON 同步带记号(HTML/下游从这读)
    data = json.loads((tmp_path / "out" / "audit-report.json").read_text(encoding="utf-8"))
    assert any(f.get("control_flow") == "quantity" for f in data["findings"])


def test_audit_suggest_default_off(monkeypatch, tmp_path):
    """--suggest 默认关:不调 build_suggestions,JSON suggestions 为 null、终端不打建议行。"""
    monkeypatch.setattr(
        "sift.cli.commands.audit.build_suggestions",
        lambda *a, **k: pytest.fail("默认关不应调 build_suggestions"),
    )
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "改进建议" not in r.output
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    assert data["suggestions"] is None


def test_audit_suggest_flag_persists_and_prints(monkeypatch, tmp_path):
    """--suggest 开:build_suggestions 挂在落盘前(报告带上建议),终端打汇总行。"""
    from sift.models.audit_models import AuditSuggestResult, AuditSuggestion

    result = AuditSuggestResult(
        suggestions=[
            AuditSuggestion(
                category="skill_fix", target_seqs=[2],
                suggestion="给约束加显式措辞", rationale="系统性没做到", keeps_pass=True,
            ),
        ],
        model_env_count=1,
        input_tokens=900, output_tokens=300, calls=4, duration_seconds=12.4,
    )
    captured: dict = {}
    engine_report = _fake_report()
    engine_report.duration_seconds = 7.2
    engine_report.token_usage = {
        "input_tokens": 100, "output_tokens": 50,
        "cache_read_input_tokens": 0, "calls": 3,
    }

    class _EngFake(_FakeAuditor):
        def audit(self, *a, **k):
            super().audit(*a, **k)
            return engine_report

    def fake_build(report, body, model=None, timeout=600, on_progress=None):
        captured["skill_body"] = body
        captured["fail"] = report.summary.get("fail")
        return result

    monkeypatch.setattr("sift.cli.commands.audit.build_suggestions", fake_build)
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _EngFake())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(
        app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out), "--suggest"],
    )
    assert r.exit_code == 0, r.output
    assert captured["fail"] == 1  # 拿到的是生成完的 report
    assert "改进建议" in r.output and "skill_fix" in r.output
    # rich 按终端宽度折行,断言先拍平空白
    flat = " ".join(r.output.split())
    assert "1 条 FAIL 归因模型/环境不出建议" in flat
    # 底部汇总是这次执行的总消耗:--suggest 跑过 → tokens/耗时并计建议段
    assert "+ 建议 input=900 output=300 · 4 call(s)" in flat
    assert "耗时: 7s + 建议生成 12s = 19s" in flat
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    assert data["suggestions"]["suggestions"][0]["category"] == "skill_fix"
    assert data["suggestions"]["model_env_count"] == 1
    # 消耗并入阶段表:真跑过 LLM(calls>0)→ 多一行 suggest(引擎外后处理单列才可见)
    sug_rows = [r_ for r_ in data["stage_breakdown"] if r_["stage"] == "suggest"]
    assert len(sug_rows) == 1
    assert sug_rows[0]["input_tokens"] == 900 and sug_rows[0]["calls"] == 4


def test_audit_suggest_tokens_line_when_engine_zero_calls(monkeypatch, tmp_path):
    """引擎零 LLM 调用(全缓存/全程序)而 --suggest 真跑了 → 建议 token 单独报。

    建议 token 行原嵌在 `if tu.get("calls"):` 内,引擎 calls=0 时被门吞掉——
    耗时行同场景是并计的,两行口径须一致(底部汇总=这次执行总消耗)。
    """
    from sift.models.audit_models import AuditSuggestResult, AuditSuggestion

    result = AuditSuggestResult(
        suggestions=[AuditSuggestion(
            category="skill_fix", target_seqs=[2],
            suggestion="加显式约束", rationale="系统性没做到", keeps_pass=True,
        )],
        input_tokens=900, output_tokens=300, calls=4, duration_seconds=12.4,
    )
    engine_report = _fake_report()
    engine_report.token_usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    class _EngFake(_FakeAuditor):
        def audit(self, *a, **k):
            super().audit(*a, **k)
            return engine_report

    monkeypatch.setattr("sift.cli.commands.audit.build_suggestions",
                        lambda *a, **k: result)
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _EngFake())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    r = runner.invoke(
        app, ["audit", str(sp), "--transcript", str(tp),
              "-o", str(tmp_path / "out"), "--suggest"],
    )
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "LLM tokens: 建议 input=900 output=300 · 4 call(s)" in flat


def test_audit_suggest_skipped_reason_prints_without_counts(monkeypatch, tmp_path):
    """门控拦下/失败也落盘空 result:终端只显原因行、不显计数(失败 ≠ 全怪模型)。"""
    from sift.models.audit_models import AuditSuggestResult

    monkeypatch.setattr(
        "sift.cli.commands.audit.build_suggestions",
        lambda report, body, model=None, timeout=600, on_progress=None: AuditSuggestResult(
            skipped_reason="UNRESOLVED 占比 2/4 > 30%,证据残缺——先重跑",
        ),
    )
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(
        app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out), "--suggest"],
    )
    assert r.exit_code == 0, r.output
    assert "改进建议未生成" in r.output
    assert "归因模型/环境" not in r.output  # 不显计数行(优先级:reason 先行)
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    assert data["suggestions"]["skipped_reason"]
    assert data["suggestions"]["suggestions"] == []
    # 门控拦下 = 零 LLM 消耗(calls=0)→ 不产 suggest 阶段行(零消耗不占行)
    assert all(r_["stage"] != "suggest" for r_ in data["stage_breakdown"])


def test_audit_console_summary_includes_token_usage(monkeypatch, tmp_path):
    # token_usage 非空 → console 打 token 行 + 写出 JSON 带字段。
    report = _fake_report()
    report.token_usage = {
        "input_tokens": 1234, "output_tokens": 56,
        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
        "total_cost_usd": 0.0234, "calls": 3,
    }

    class _TokFake(_FakeAuditor):
        def audit(self, *a, **k):
            super().audit(*a, **k)  # 记录入参
            return report

    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _TokFake())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "LLM tokens" in r.output
    assert "1234" in r.output  # input_tokens
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    assert data["token_usage"]["calls"] == 3
    assert data["token_usage"]["input_tokens"] == 1234


def test_audit_console_summary_omits_token_usage_when_empty(monkeypatch, tmp_path):
    # token_usage 空(默认)→ 不打 token 行(别出 tokens=0 噪声)。
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = _transcript(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "LLM tokens" not in r.output
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    assert data["token_usage"] == {}


def test_audit_multiple_transcripts_repeatable(monkeypatch, tmp_path):
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
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
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
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
    # SkillParseError 是 InputError 子类(exit_code=2)→ 被 except SiftError 捕获
    assert r.exit_code == 2
    # console.print 过错误信息(证明走了友好错误路径,而非裸异常)
    assert "SKILL.md" in r.output
    assert "Error" in r.output


def test_audit_shared_agents_dir_hints_pass_specific_file(tmp_path):
    """传装多个 agent .md 的共享目录(无 SKILL.md)→ 友好提示传具体文件(真实 agent 布局误用)。"""
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "developer.md").write_text("---\nname: developer\ndescription: d\n---\nbody", encoding="utf-8")
    (agents / "reviewer.md").write_text("---\nname: reviewer\ndescription: r\n---\nbody", encoding="utf-8")
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(agents), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code == 2
    assert "直接传具体文件路径" in r.output
    assert "developer.md" in r.output  # 点出真实存在的 .md


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
        "sift.cli.commands.audit.TranscriptAuditor",
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
        "sift.cli.commands.audit.TranscriptAuditor",
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


# ── --kind 未指定时的对称收窄提示(skill/agent/root 一视同仁)。 ──


def _skill_use_rec(name: str) -> str:
    """一条调 Skill(name)的 cc record(jsonl 行)。"""
    return json.dumps({"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": name}}]}})


def _agent_rec(agent: str) -> str:
    """一条带 agent 归属字段的 record(structured reader 展开后的 cc-record 形)。"""
    return json.dumps({"agent": agent, "message": {"role": "assistant", "content": [
        {"type": "text", "text": f"{agent} 干活"}]}})


def test_audit_no_kind_hints_scope_when_skill_invoked(monkeypatch, tmp_path):
    """--kind 未指定 + transcript 调了本 skill → 黄字提示可加 --kind skill(对称收窄,非阻断)。"""
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)  # name=myskill
    tp = tmp_path / "t.jsonl"
    tp.write_text(_skill_use_rec("myskill"), encoding="utf-8")
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    # 提示出现 + 含本 skill 名 + 仍照常宽松对账(非阻断)
    assert "收窄" in r.output and "--kind" in r.output and "myskill" in r.output
    assert "Audited" in r.output  # 宽松对账照跑


def test_audit_passes_frontmatter_tools_to_engine(monkeypatch, tmp_path):
    """声明 frontmatter 的 tools:/allowed-tools: 白名单由 CLI 抽好传引擎(越界视图
    第四声明源);普通 skill(无该字段)→ 空 dict,引擎零影响。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)
    sp = tmp_path / "agent"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: a\ndescription: d\ntools:\n  - Read\n  - mcp__issues__search\n---\n# body\n",
        encoding="utf-8",
    )
    tp = _transcript(tmp_path)
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert fake.calls[0]["declared_tools"] == {
        "read": "frontmatter: tools", "mcp__issues__search": "frontmatter: tools",
    }

    # 无 frontmatter 工具字段的普通 skill → 空 dict
    fake2 = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake2)
    sp2 = _skill_dir(tmp_path)
    r2 = runner.invoke(app, ["audit", str(sp2), "--transcript", str(tp), "-o", str(out)])
    assert r2.exit_code == 0, r2.output
    assert fake2.calls[0]["declared_tools"] == {}


def test_audit_agent_fixture_frontmatter_boundary_end_to_end(monkeypatch, tmp_path):
    """committed agent fixture(issue-triage-agent):frontmatter tools 是唯一声明源
    (正文零工具名),--kind agent 整链 → mcp__issues__search=domain、
    mcp__slack__post=undeclared-domain;切段落盘。fixture 被改坏时这里先红。"""
    monkeypatch.setattr(
        "sift.audit.auditor.call_claude",
        lambda *a, **k: '[{"index":0,"applicable":true,"violated":false,"verdict":"pass","evidence":"未违规"}]',
    )
    base = Path(__file__).parent.parent / "fixtures" / "example-agents" / "issue-triage-agent"
    out = tmp_path / "out"
    r = runner.invoke(app, [
        "audit", str(base / "issue-triage-agent.md"),
        "--transcript", str(base / "transcripts" / "synthetic-mcp.jsonl"),
        "--kind", "agent", "-o", str(out),
    ])
    assert r.exit_code == 0, r.output
    data = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    b = data["boundary"]
    assert b["declared"] == 1 and b["undeclared"] == 1
    by_name = {t["name"]: t for t in b["tools"]}
    assert by_name["mcp__issues__search"]["classification"] == "domain"
    assert by_name["mcp__issues__search"]["declared_by"] == "frontmatter: tools"
    assert by_name["mcp__slack__post"]["classification"] == "undeclared-domain"
    assert list((out / "slices").glob("*issue-triage-agent*"))


def test_audit_no_kind_hints_scope_when_agent_present(monkeypatch, tmp_path):
    """--kind 未指定 + transcript 有 agent==name 段 → 提示 --kind agent。"""
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)  # name=myskill
    tp = tmp_path / "t.jsonl"
    tp.write_text(_agent_rec("myskill"), encoding="utf-8")  # agent 字段 == 声明名
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "收窄" in r.output and "--kind agent" in r.output


def test_audit_no_kind_hints_scope_when_subagent_dispatched(monkeypatch, tmp_path):
    """--kind 未指定 + transcript 有子 agent 作用域 → 提示 --kind root。"""
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    a_id = "call_A"
    recs = [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "main"}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": a_id, "name": "Agent", "input": {"subagent_type": "general-purpose"}}]}},
        {"parent_tool_use_id": a_id, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "sub"}]}},
    ]
    tp.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "收窄" in r.output and "--kind root" in r.output


def test_audit_no_kind_no_hint_when_no_anchors(monkeypatch, tmp_path):
    """--kind 未指定 + transcript 无任何收窄锚点(没调本 skill / 没 name agent / 没子 agent)
    → 不提示(宽松是唯一选项)。"""
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text(json.dumps({"message": {"role": "assistant", "content": [
        {"type": "text", "text": "just doing main work, no skill/agent/subagent"}]}}),
        encoding="utf-8")
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "收窄" not in r.output


def test_audit_with_kind_no_scope_hint(monkeypatch, tmp_path):
    """指定了 --kind(任一)→ 不打收窄提示(用户已选精确档,哪怕 transcript 含锚点)。"""
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _FakeAuditor())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"
    tp.write_text(_skill_use_rec("myskill"), encoding="utf-8")  # 含 skill 锚点
    out = tmp_path / "out"

    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(tp), "--kind", "skill", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "收窄" not in r.output  # 带 --kind → 不提示


from sift.cli.commands.audit import format_unresolved_breakdown


class TestFormatUnresolvedBreakdown:
    def test_empty_returns_empty(self):
        assert format_unresolved_breakdown({}) == ""

    def test_orders_by_count_desc(self):
        s = format_unresolved_breakdown({"rate_limited": 1, "timeout": 2})
        assert s.index("2×") < s.index("1×")  # 计数大的在前
        assert "LLM 超时" in s and "限流" in s  # 标签来自 unresolved_cause_info

    def test_unknown_cause_uses_fallback_label(self):
        s = format_unresolved_breakdown({"weird": 1})
        assert "1×" in s and "未知" in s
