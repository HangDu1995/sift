# tests/test_agent_audit_example.py
"""agent-audit 的 example([fixtures/example-agents/](../fixtures/example-agents/))集成守卫。

example 是**可运行示例**(不是测试夹具):两个域无关 agent(test-runner / lint-reviewer,
以 `<name>/SKILL.md` 放,可直接 `skill-eval audit` 跑)+ 一份 multi-agent session
([multi-agent-session.json](../fixtures/example-agents/multi-agent-session.json):main
dispatch test-runner → lint-reviewer → 再 test-runner,每段种入可抓的违规)。

本文件直接引用那份 example(单一来源)验证 agent-audit 这条路径成立:
- **多 agent 切段隔离**:审 test-runner 的段不含 lint-reviewer 的 turn、反之亦然;
  test-runner 二次 dispatch → 切两段。
- **example 抽出真指令**:test-runner 出 pytest 工具要求 + 严禁/禁止 约束 + 禁止捏造;
  lint-reviewer 出 `./lint.sh`(验 ``./`` 前缀)+ ruff。
- **全管线跑通**:mock ``call_claude`` 跑真 ``TranscriptAuditor.audit``,pytest 工具要求
  经 ``match_tool_call`` 程序命中 → PASS(不调 LLM)。
- **真 LLM 混合 verdict**(env-gate):实跑确认种下的违规(pytest -x)被 FAIL 抓到。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skill_eval.cli.main import app
from skill_eval.compliance.extractor import InstructionExtractor

runner = CliRunner()
EX = Path(__file__).parent.parent / "fixtures" / "example-agents"
SESSION = EX / "multi-agent-session.json"


# ---- 辅助 -----------------------------------------------------------------

class _CapturingAuditor:
    """记录 auditor 收到的切段后 transcript(不真跑 LLM),供断言切段隔离。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600,
              on_progress=None, refine_instructions=False, locate_turns=False):
        captured = []
        for tp in transcripts:
            try:
                captured.append(Path(tp).read_text(encoding="utf-8"))
            except OSError:
                captured.append("")
        self.calls.append({"skill_name": skill_name, "segments": captured})
        from skill_eval.models.audit_models import AuditReport
        return AuditReport(skill_name=skill_name, transcripts=[str(tp) for tp in transcripts],
                           summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
                           findings=[], warnings=[])


def _run_capturing(monkeypatch, tmp_path, agent: str) -> _CapturingAuditor:
    """对 example 的 <agent> 跑 audit(mock 掉 auditor 抓切段后内容),返回 capture。"""
    fake = _CapturingAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)
    out = tmp_path / f"out-{agent}"
    r = runner.invoke(app, ["audit", str(EX / agent), "--transcript", str(SESSION),
                            "--kind", "agent", "-o", str(out)])
    assert r.exit_code == 0, r.output
    return fake


def _segments_joined(fake: _CapturingAuditor) -> str:
    return "\n".join(seg for c in fake.calls for seg in c["segments"])


# ---- 1. 多 agent 切段隔离 --------------------------------------------------

def test_multi_agent_isolation(monkeypatch, tmp_path):
    """审 test-runner 的段只含 test-runner 的 turn(pytest),不含 lint-reviewer 的(ruff/lint.sh);
    审 lint-reviewer 反之。按 ``agent`` 字段精确隔离,不靠工具类型猜。"""
    tr = _run_capturing(monkeypatch, tmp_path, "test-runner")
    tr_joined = _segments_joined(tr)
    assert "pytest" in tr_joined
    assert "ruff" not in tr_joined and "lint.sh" not in tr_joined

    lr = _run_capturing(monkeypatch, tmp_path, "lint-reviewer")
    lr_joined = _segments_joined(lr)
    assert "ruff" in lr_joined and "lint.sh" in lr_joined
    assert "pytest" not in lr_joined


# ---- 2. 二次 dispatch → 两段 -----------------------------------------------

def test_redispatch_yields_two_segments(monkeypatch, tmp_path):
    """test-runner 被 dispatch 两次(中间隔着 lint-reviewer)→ 切两段,不合并、不漏。"""
    tr = _run_capturing(monkeypatch, tmp_path, "test-runner")
    segs = [seg for c in tr.calls for seg in c["segments"]]
    assert len(segs) == 2


# ---- 3. example 抽出真指令 -------------------------------------------------

def test_example_agents_yield_checkable_instructions():
    """example 是有实质内容的 agent(非 toy):抽出可核验的 TOOL_REQUIREMENT / CONSTRAINT /
    ANTI_FABRICATION。顺带验 extractor 改动(``跑``/``使用`` 动词 + ``./`` 前缀)。"""
    def body(agent: str) -> str:
        return (EX / agent / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[2]

    tr = InstructionExtractor().extract(body("test-runner"))
    tr_tools = [c.params.get("tool_name") for c in tr if c.source.name == "TOOL_REQUIREMENT"]
    tr_constraints = "".join(c.instruction_text for c in tr if c.source.name == "CONSTRAINT")
    assert any("pytest" in (t or "") for t in tr_tools), tr_tools
    assert "严禁" in tr_constraints and "不得" in tr_constraints
    assert any(c.source.name == "ANTI_FABRICATION" for c in tr)

    lr = InstructionExtractor().extract(body("lint-reviewer"))
    lr_tools = [c.params.get("tool_name") for c in lr if c.source.name == "TOOL_REQUIREMENT"]
    assert any("lint.sh" in (t or "") for t in lr_tools), lr_tools     # 使用 `./lint.sh` → lint.sh
    assert any("ruff" in (t or "") for t in lr_tools)


# ---- 4. 全管线跑通(真 auditor,mock call_claude)---------------------------

def _fake_call_claude():
    """通用 mock:禁令 violated=true→FAIL、步骤 verdict=pass→PASS、条件性 applicable=false→N/A
    (各解析器只读自己关心的键,见 test_audit_integration._make_fake)。"""
    def fake(prompt, *a, **k):
        return json.dumps([{"index": i, "applicable": False, "violated": True,
                            "verdict": "pass", "evidence": "agent-audit example mock"} for i in range(60)])
    return fake


def test_audit_pipeline_runs_on_agent_example(tmp_path, monkeypatch):
    """example agent + session → 真 ``TranscriptAuditor.audit`` 全流程(mock LLM 边界):
    pytest 工具要求经 ``match_tool_call`` 程序命中 → PASS(不调 LLM)。证明 agent-audit 端到端
    跑通,且程序判定能在 agent 段上确认"该跑的工具跑了"。"""
    monkeypatch.setattr("skill_eval.audit.auditor.time.sleep", lambda *a, **k: None)
    monkeypatch.setattr("skill_eval.audit.auditor.call_claude", _fake_call_claude())
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(EX / "test-runner"), "--transcript", str(SESSION),
                            "--kind", "agent", "-o", str(out)])
    assert r.exit_code == 0, r.output

    report = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    pytest_findings = [f for f in report.get("findings", [])
                       if "pytest" in (f.get("instruction_text") or "").lower()
                       and "TOOL" in str(f.get("source", ""))]
    assert pytest_findings, "应抽出 pytest 工具要求"
    assert any(f.get("verdict") == "PASS" for f in pytest_findings), \
        [f.get("verdict") for f in pytest_findings]


# ---- 5. 真 LLM 混合 verdict(env-gate)-------------------------------------

def test_full_audit_real_llm(tmp_path):
    """真 LLM 跑全量对账:确认种下的违规(pytest -x 违反「严禁 -x」)被 FAIL 抓到、遵守的 PASS
    ——agent-audit 在真实多 agent session 上的价值落地。无 ``ANTHROPIC_*`` 时 skip。"""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        pytest.skip("needs ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN for real LLM audit")
    out = tmp_path / "out-live"
    r = runner.invoke(app, ["audit", str(EX / "test-runner"), "--transcript", str(SESSION),
                            "--kind", "agent", "-o", str(out)], catch_exceptions=False)
    assert r.exit_code == 0, r.output
    report = json.loads((out / "audit-report.json").read_text(encoding="utf-8"))
    summary = report.get("summary", {})
    assert summary.get("pass", 0) >= 1 and summary.get("fail", 0) >= 1, summary   # 混合 verdict
