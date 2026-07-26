# tests/test_audit_by_skill_e2e.py
"""audit --kind skill 端到端 CLI smoke:在 committed 多 skill fixture 上跑全管线(mock LLM)。

与 ``test_audit_by_skill.py`` 的层次区别:那里 mock **整个 TranscriptAuditor**(只验 CLI
接线 / 切片派发);这里只 mock ``call_claude``,**真跑 ``TranscriptAuditor.audit`` 全流程**
(切片 → rebuild → extract → derive → 四判定 → 聚合 → 落盘),on the committed
``fixtures/example-skills/multi-skill-demo/multi-skill.jsonl`` fixture。

只断言**管线 plumbing**,不断言 verdict:
  - fixture 植入的"违规"非确定性——lea/cra 的输出要求是散文(extractor 只对代码块抽
    OUTPUT_FORMAT → 这俩零条);rcz 的 OUTPUT 检查对 assistant 措辞不对写入文件。故
    verdict 多为 LLM 辅助 / spurious,本测试不绑 FAIL/PASS 计数。
  - 本测试验的是 --kind skill 本身保证的东西:切片**喂进**了 auditor、切片数 == 该 skill
    被调次数、report.transcripts == 落盘切片、切片纯度(CRA 领域文件操作不串入 LEA 切片)。

mock 设计照 ``test_audit_integration.py._make_fake``:fake 返回覆盖 0..59 的通用 JSON
数组,每条同时带 ``applicable``/``violated``/``verdict``/``evidence`` 四键,三个 LLM 判定
方法各只读自己关心的键,一个结构兼容三种格式,无需按 prompt 内容分支。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from skill_eval.cli.main import app
from skill_eval.models.audit_models import AuditReport

runner = CliRunner()

FIXTURE_TRANSCRIPT = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "example-skills"
    / "multi-skill-demo"
    / "multi-skill.jsonl"
)
FIXTURE_SKILLS = Path(__file__).resolve().parent.parent / "fixtures" / "example-skills"

LEA = "log-error-analyzer"
CRA = "code-review-auditor"
RCZ = "release-checklist-zh"

# 各 skill 在 fixture 里的调用次数(= 切片段数 = slice 文件数)。
# lea 被调两次(Step1 + Step4);cra 主作用域一次 + 子 agent 一次(Step2 + Step5);
# rcz 一次(Step3)。与 test_multi_skill_fixture.py 的段数断言一致。
EXPECTED_SLICES = {LEA: 2, CRA: 2, RCZ: 1}


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(monkeypatch) -> None:
    """重试退避的 time.sleep → no-op(照 test_audit_integration.py,保持闭环测试快)。"""
    monkeypatch.setattr("skill_eval.audit.auditor.time.sleep", lambda *a, **k: None)


def _make_fake() -> tuple:
    """通用 mock call_claude,兼容三种判定格式(照 test_audit_integration.py._make_fake)。

    返回 (fake, calls):fake 输出覆盖 0..59 的 JSON 数组,每条同时带
    ``applicable``/``violated``/``verdict``/``evidence``。三个判定器各只读自己关心的键
    (applicable 路径读 applicable、禁令读 violated、步骤读 verdict),互不干扰。
    全给"通过"语义(applicable=false / violated=false / verdict=pass)使管线平滑走完、
    不产假 FAIL——本测试不验 verdict,只验 plumbing。
    """
    calls = {"n": 0}

    def fake(prompt, *a, **k):
        calls["n"] += 1
        return json.dumps([
            {
                "index": i,
                "applicable": False,
                "violated": False,
                "verdict": "pass",
                "evidence": "e2e mock 判定(通过)",
            }
            for i in range(60)
        ])

    return fake, calls


# ---------------------------------------------------------------------------
# plumbing:三个 skill 各跑一遍 audit --kind skill 全管线
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "skill_name", [LEA, CRA, RCZ], ids=["lea", "cra", "rcz"]
)
def test_by_skill_e2e_plumbing(skill_name, monkeypatch, tmp_path) -> None:
    """单 skill 端到端:committed fixture + 真 auditor(只 mock LLM)→ 全管线落盘。

    断言(只 plumbing,不绑 verdict):
      1. exit code 0。
      2. audit-report.json 落盘且能解析为 AuditReport,skill_name 对。
      3. slices/ 下切片文件数 == 该 skill 被调次数(lea=2 / cra=2 / rcz=1)。
      4. report.transcripts == 落盘切片文件 basename 集合(证明切片真喂进了 auditor)。
      5. 切片文件确在盘上。
    """
    fake, _calls = _make_fake()
    monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)

    skill_dir = FIXTURE_SKILLS / skill_name
    out = tmp_path / skill_name

    r = runner.invoke(
        app,
        [
            "audit", str(skill_dir),
            "--transcript", str(FIXTURE_TRANSCRIPT),
            "--kind", "skill", "-o", str(out),
        ],
    )
    assert r.exit_code == 0, r.output

    # 1. report 落盘且解析为 AuditReport
    report_path = out / "audit-report.json"
    assert report_path.exists(), f"audit-report.json 未落盘: {report_path}"
    report = AuditReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    assert isinstance(report, AuditReport)
    assert report.skill_name == skill_name

    # 2. slices/ 下切片数 == 该 skill 调用次数
    slice_dir = out / "slices"
    assert slice_dir.is_dir(), f"slices/ 目录不存在: {slice_dir}"
    slice_files = sorted(slice_dir.glob("*.jsonl"))
    expected = EXPECTED_SLICES[skill_name]
    assert len(slice_files) == expected, (
        f"{skill_name}: 预期 {expected} 个切片,实际 {len(slice_files)} "
        f"({[p.name for p in slice_files]})"
    )

    # 3. report.transcripts == 落盘切片 basename 集合(切片喂进了 auditor)
    slice_basenames = sorted(p.name for p in slice_files)
    assert sorted(report.transcripts) == slice_basenames, (
        f"{skill_name}: report.transcripts 与落盘切片不一致\n"
        f"  report={sorted(report.transcripts)}\n  slices ={slice_basenames}"
    )

    # 4. 切片文件确在盘上
    for p in slice_files:
        assert p.exists(), f"切片文件不在盘上: {p}"


# ---------------------------------------------------------------------------
# 切片纯度(经全 CLI 管线):LEA 切片不含 CRA 领域文件操作
# ---------------------------------------------------------------------------


def _file_ops_text(span_records: list[dict]) -> str:
    """提取段内文件操作文本(Read/Write/Edit 的 file_path + Bash 的 command)。

    与 ``test_multi_skill_fixture.py._file_ops`` 同口径:**不抽 Agent tool_use**——
    Agent 的 prompt 字符串可能提及其他 skill 的文件(如主 agent 在 LEA 段里 dispatch
    子 agent 去 review src/orders.py),那是合法派发、非切片串味。切片纯度看的是**真实
    文件操作**(Read/Write/Edit/Bash)是否跨 skill 串入,这才是 --kind skill 保证的属性。
    """
    parts: list[str] = []
    for rec in span_records:
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name, inp = b.get("name"), b.get("input") or {}
                if name in ("Read", "Write", "Edit"):
                    parts.append(inp.get("file_path", ""))
                elif name == "Bash":
                    parts.append(inp.get("command", ""))
    return " ".join(parts)


def test_lea_slices_no_cra_file_contamination(monkeypatch, tmp_path) -> None:
    """LEA 的 CLI 切片里不含 CRA 领域文件操作(src/payment.py / src/orders.py)。

    这是 --kind skill 的核心保证(切片不串味)经**全 CLI 管线**的复验(对应
    test_multi_skill_fixture.py 的纯度单测,但读的是 CLI 落盘的切片文件、走真 auditor)。

    注:src/orders.py 会出现在 LEA span2 的 **Agent tool_use prompt** 里(主 agent 在做
    LEA 工作时 dispatch 子 agent 去 review orders 模块)——那是合法派发不是串味,故本断言
    检文件操作(Read/Write/Edit/Bash)而非裸子串。src/payment.py 在 LEA 切片里连裸子串都
    不出现(CRA 主作用域的 Read,与 LEA 段完全隔离)。
    """
    fake, _calls = _make_fake()
    monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)

    skill_dir = FIXTURE_SKILLS / LEA
    out = tmp_path / "lea-purity"

    r = runner.invoke(
        app,
        [
            "audit", str(skill_dir),
            "--transcript", str(FIXTURE_TRANSCRIPT),
            "--kind", "skill", "-o", str(out),
        ],
    )
    assert r.exit_code == 0, r.output

    slice_dir = out / "slices"
    slice_files = sorted(slice_dir.glob("*.jsonl"))
    assert slice_files, "LEA 应至少有一个切片文件"
    assert len(slice_files) == EXPECTED_SLICES[LEA]

    # 逐切片读 jsonl → 解析 record → 抽文件操作 → 断言 CRA 领域文件不串入
    cra_domain = ("src/payment.py", "src/orders.py")
    for sp in slice_files:
        records = [
            json.loads(line)
            for line in sp.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        blob = _file_ops_text(records)
        for bad in cra_domain:
            assert bad not in blob, (
                f"LEA 切片 {sp.name} 被 CRA 文件操作污染:出现 {bad!r}\n"
                f"  file_ops: {blob}"
            )

    # 补充:src/payment.py 在 LEA 切片里连裸子串都不出现(CRA 主作用域 Read,完全隔离)。
    # (src/orders.py 在 Agent prompt 里有裸子串,故不查它的裸子串。)
    all_raw = "\n".join(sp.read_text(encoding="utf-8") for sp in slice_files)
    assert "src/payment.py" not in all_raw, "src/payment.py 不应出现在 LEA 任何切片里"
