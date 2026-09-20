"""tests/test_audit_integration.py — audit 管线闭环集成测试。

与 ``test_auditor.py`` 的**单组件**测试不同,这里**真跑 ``TranscriptAuditor.audit``
全流程**(rebuild → extract → derive → classify → 四判定 → 聚合),只 mock LLM 边界
(``call_claude``):合成 SKILL.md body + 合成 transcript JSONL,断言四类 verdict 都能
正确产出、summary 计数一致、反推声明被标 derived。

合成设计(读 ``compliance/extractor.py`` 确认各 source 触发模式,确保指令真能被抽出;
读 ``evals/transcript.py`` 的 JSONL 格式,确保 transcript 真能 rebuild):

- ``CONSTRAINT``      "NEVER delete production data"     → PROHIBITION 桶
                     (mock violated=true  → FAIL,违规检出)
- ``TOOL_REQUIREMENT`` "use Bash"                        → CONDITIONAL 桶
                     (transcript 含 Bash 调用 → 命中 match_tool_call → PASS,**不调 LLM**)
- ``HEADING_STEP``    "1. Analyze the input format ..."  → STEP 桶
                     (mock verdict="pass" → PASS)
- ``OUTPUT_FORMAT``   代码块 ``EXPECTED_TOKEN``           → OUTPUT 桶
                     (确定性子串命中 transcript_text → PASS,method=PROGRAMMATIC)
- transcript 另 Read ``notes.md``(不在 skill_body)      → ``derive_declarations`` 反推
                     "读取 notes.md"(``REFERENCE_USAGE``),命中 Read → PASS,
                     标 ``derived_from_transcript=True``

mock 设计(与 ``test_auditor.py._pass_fake`` 同思路):fake 返回覆盖 0..59 的通用 JSON
数组,每条**同时**带 ``applicable``/``violated``/``verdict``/``evidence`` 四键。三个 LLM
判定方法各只读自己关心的键(``_parse_applicable`` 读 applicable、``_parse_prohibition``
读 violated、``_parse_step`` 读 verdict),一个结构兼容三种格式,无需按 prompt 内容分支。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sift.audit.auditor import TranscriptAuditor
from sift.audit.judge_context import AuditOptions
from sift.models.audit_models import (
    AuditCategory,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(monkeypatch) -> None:
    """重试退避的 time.sleep → no-op,保持闭环测试快(retry 逻辑由 test_auditor 覆盖)。"""
    monkeypatch.setattr("sift.audit.auditor.time.sleep", lambda *a, **k: None)


# ---- 合成 transcript JSONL 辅助(仿 test_transcript.py / test_auditor.py)---------


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """把 records 写成 JSONL(每行一个 JSON 对象),返回该路径。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _user(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}


def _asst_text(text: str) -> dict:
    """纯文本 assistant 块(进 digest.transcript_text,供 OUTPUT 子串命中)。"""
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _asst(tool_uses: list) -> dict:
    return {"message": {"role": "assistant", "content": tool_uses}}


def _tool_use(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result(tid: str, text: str) -> dict:
    return {
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
        }
    }


# ---- 合成 SKILL.md body:四类指令各一条,触发 extractor 各 source ---------------

# (先在 extractor 上验证过:恰好抽出 1 TOOL_REQUIREMENT + 1 CONSTRAINT +
#  1 HEADING_STEP + 1 OUTPUT_FORMAT,无 REFERENCE_USAGE / ANTI_FABRICATION 误抽)
SKILL_BODY = (
    "use Bash\n\n"
    "NEVER delete production data\n\n"
    "## 步骤一:分析问题\n"
    "1. Analyze the input format carefully\n\n"
    "## 输出格式\n"
    "```\n"
    "EXPECTED_TOKEN\n"
    "```\n"
)


def _make_fake() -> tuple:
    """返回 ``(fake, calls)``:通用 mock call_claude,兼容三种判定格式。

    - 禁令 → ``violated=true``  → FAIL(违规检出)
    - 步骤 → ``verdict="pass"`` → PASS(走过该步)
    - 条件性未命中 → ``applicable=false`` → N/A(场景不涉及)

    各解析器只读自己关心的键,互不干扰。``calls["n"]`` 计 LLM 被调次数,便于断言
    "命中 tool_trace 的条件性指令不调 LLM、禁令/步骤才调"。
    """
    calls = {"n": 0}

    def fake(prompt, *a, **k):
        calls["n"] += 1
        return json.dumps(
            [
                {
                    "index": i,
                    "applicable": False,
                    "violated": True,
                    "verdict": "pass",
                    "evidence": "集成测试 mock 判定",
                }
                for i in range(60)
            ]
        )

    return fake, calls


# ---------------------------------------------------------------------------
# 闭环主测试:四类 verdict + summary + derived + LLM 边界
# ---------------------------------------------------------------------------


class TestAuditEndToEnd:
    """SKILL.md + transcript → audit 全流程 → 四类 verdict + summary + derived。"""

    @staticmethod
    def test_full_pipeline_four_categories(tmp_path, monkeypatch) -> None:
        fake, calls = _make_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)

        # 合成 transcript:user turn + 含 EXPECTED_TOKEN 的 assistant text(供 OUTPUT
        # 子串命中)+ Bash 调用(供 "use Bash" 命中)+ Read notes.md(不在 skill_body
        # → derive_declarations 反推"读取 notes.md",且命中 Read → PASS-derived)
        records = [
            _user("请分析问题并输出结果"),
            _asst_text("先分析,EXPECTED_TOKEN 是期望输出标记"),
            _asst([_tool_use("b1", "Bash", {"command": "echo hi", "description": ""})]),
            _tool_result("b1", "hi\n"),
            _asst([_tool_use("r1", "Read", {"file_path": str(tmp_path / "notes.md")})]),
            _tool_result("r1", "1\tsome notes\n"),
        ]
        tp = _write_jsonl(tmp_path / "session.jsonl", records)

        # 也把 body 写成 SKILL.md 文件(证明 body 来自真实 skill 文件),读回后喂 audit
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(SKILL_BODY, encoding="utf-8")
        body = skill_file.read_text(encoding="utf-8")

        report = TranscriptAuditor().audit(
            skill_name="demo-skill", skill_body=body, transcripts=[tp], options=AuditOptions()
        )

        # ---- AuditReport 基本字段 ----
        assert isinstance(report, AuditReport)
        assert report.skill_name == "demo-skill"
        assert report.transcripts == ["session.jsonl"]  # 含合成 transcript 文件名
        assert report.warnings == []

        # ---- 四类 verdict 各至少一条(端到端走完四条 _judge_*)----
        _assert_four_audit_categories(report)

        derived = [f for f in report.findings if f.derived_from_transcript]
        assert len(derived) == 1
        assert derived[0].category == AuditCategory.CONDITIONAL
        assert derived[0].verdict == AuditVerdict.PASS
        assert "notes.md" in derived[0].instruction_text

        # ---- summary 与 findings 逐项一致 ----
        expected = {
            "total": len(report.findings),
            "pass": sum(1 for f in report.findings if f.verdict == AuditVerdict.PASS),
            "fail": sum(1 for f in report.findings if f.verdict == AuditVerdict.FAIL),
            "na": sum(1 for f in report.findings if f.verdict == AuditVerdict.NA),
            "unresolved": sum(1 for f in report.findings if f.verdict == AuditVerdict.UNRESOLVED),
            "indeterminate": sum(1 for f in report.findings if f.verdict == AuditVerdict.INDETERMINATE),
        }
        assert report.summary == expected
        # 明确数值:5 = 2 CONDITIONAL(Bash 命中 + 反推 notes.md 命中)
        #              + 1 PROHIBITION(violated) + 1 STEP(pass) + 1 OUTPUT(子串命中)
        assert report.summary == {
            "total": 5,
            "pass": 4,
            "fail": 1,
            "na": 0,
            "unresolved": 0,
            "indeterminate": 0,
        }

        # ---- LLM 边界:条件性两条全命中(不调 LLM),只有禁令 + 步骤各调一次 = 2 次
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# N/A 路径:条件性未命中 + 场景不触发 → N/A(端到端)
# ---------------------------------------------------------------------------


class TestConditionalNaPath:
    """条件性指令未命中 tool_trace + LLM 判场景不涉及 → verdict=N/A(端到端)。"""

    @staticmethod
    def test_unmatched_applicable_false_is_na(tmp_path, monkeypatch) -> None:
        fake, calls = _make_fake()
        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)

        # skill_body 仅一条 TOOL_REQUIREMENT;transcript 无 Bash 调用 → 未命中 → 走 LLM
        body = "use Bash\n"
        records = [
            _user("一个不涉及 Bash 的场景"),
            _asst_text("仅文本回复,没调任何工具"),
        ]
        tp = _write_jsonl(tmp_path / "no_bash.jsonl", records)

        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

        cond = [f for f in report.findings if f.category == AuditCategory.CONDITIONAL]
        assert len(cond) == 1
        f = cond[0]
        assert f.verdict == AuditVerdict.NA  # 未命中 + LLM applicable=false → N/A
        assert f.source == "TOOL_REQUIREMENT"
        assert "场景不涉及" in f.evidence
        # summary 的 na 计数与 findings 一致
        assert report.summary == {
            "total": 1,
            "pass": 0,
            "fail": 0,
            "na": 1,
            "unresolved": 0,
            "indeterminate": 0,
        }
        # 未命中 → N/A 来自 LLM 判定(非命中短路),即 applicable 路径确实被走过。
        # (auditor 对空桶也调一次 call_claude、不在空 batch 短路,故实际调用 ≥1;
        #  这里只断言"走过 LLM"这一事实,不绑定空桶调用次数以免耦合实现细节。)
        assert calls["n"] >= 1


# ---------------------------------------------------------------------------
# LLM 判定失败(transient 不可用)→ 不报假 FAIL,标 N/A + 警告(宁少报不乱报)
# ---------------------------------------------------------------------------


class TestUnverifiableNoFalseFail:
    """LLM 不可用时绝不产假 FAIL,而是 N/A + "未判定"警告。

    回归 audit-out2 实况:LLM 抖动一次,49 条全保守 FAIL = 乱报,结果不可信。修复后
    判不准 → N/A(低置信度),summary.fail 只含 LLM 明确判的违规;低置信度 N/A 由
    audit() 汇总成警告,让人知道本次结果可能不完整(而非误读"0 FAIL = 全过")。
    """

    @staticmethod
    def test_llm_down_no_false_fail_but_warns(tmp_path, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("claude CLI down")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        # 不含 OUTPUT_FORMAT(确定性判定,会真 FAIL,干扰"LLM 挂→0 假 FAIL"的隔离):
        # 只放条件性 + 禁令 + 步骤,三者都走 LLM → 全挂 → 全 N/A。
        body = "use Bash\n\nNEVER delete production data\n\n## 步骤一:分析问题\n1. Analyze the input format carefully\n"
        tp = _write_jsonl(tmp_path / "down.jsonl", [_user("某场景"), _asst_text("执行记录")])

        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())

        # 关键:LLM 全挂 → 0 假 FAIL(旧保守 FAIL 会产出 N 条 FAIL = 乱报)
        assert report.summary["fail"] == 0
        assert report.summary["pass"] == 0
        # 三条指令(条件性未命中 + 禁令 + 步骤)全标 UNRESOLVED(没判成)
        assert report.summary["unresolved"] == 3
        assert report.summary["na"] == 0
        # 全是低置信度兜底 N/A(非 LLM 明确判的 resolved N/A=0.8)
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in report.findings)
        # 低置信度 N/A 汇总成警告:提示结果不完整、建议重跑
        assert len(report.warnings) == 1
        assert "3 条" in report.warnings[0]
        assert "建议重跑" in report.warnings[0]


def _assert_four_audit_categories(report: AuditReport) -> None:
    """保留四种判定路径各自的结论、来源、方法及 Bash 证据断言。"""
    cats = {f.category for f in report.findings}
    assert cats == {
        AuditCategory.CONDITIONAL,
        AuditCategory.PROHIBITION,
        AuditCategory.STEP,
        AuditCategory.OUTPUT,
    }

    # CONDITIONAL:命中 tool_trace → PASS,确定性(不调 LLM),method=PROGRAMMATIC
    cond_pass = [
        f for f in report.findings if f.category == AuditCategory.CONDITIONAL and f.verdict == AuditVerdict.PASS
    ]
    assert cond_pass, "应至少一条 CONDITIONAL PASS(命中 tool_trace)"
    assert all(f.method == AuditMethod.PROGRAMMATIC for f in cond_pass)

    # 其中 "use Bash" 来自 skill_body 静态指令(非反推),evidence 提及 Bash
    bash_f = next(f for f in cond_pass if "use Bash" in f.instruction_text)
    assert bash_f.derived_from_transcript is False
    assert bash_f.source == "TOOL_REQUIREMENT"
    assert "Bash" in bash_f.evidence

    # PROHIBITION:mock violated=true → FAIL(违规检出)
    proh = [f for f in report.findings if f.category == AuditCategory.PROHIBITION]
    assert len(proh) == 1
    assert proh[0].verdict == AuditVerdict.FAIL
    assert proh[0].source == "CONSTRAINT"

    # 步骤判定：模拟模型确认已执行该步骤，预期通过。
    step = [f for f in report.findings if f.category == AuditCategory.STEP]
    assert len(step) == 1
    assert step[0].verdict == AuditVerdict.PASS

    # OUTPUT:确定性子串(EXPECTED_TOKEN 在 transcript_text)→ PASS,method=PROGRAMMATIC
    out = [f for f in report.findings if f.category == AuditCategory.OUTPUT]
    assert len(out) == 1
    assert out[0].verdict == AuditVerdict.PASS
    assert out[0].method == AuditMethod.PROGRAMMATIC

    # ---- 反推声明:Read notes.md 不在 skill_body → derive_declarations 反推
    # "读取 notes.md"(REFERENCE_USAGE),命中 Read → PASS,标 derived=True
