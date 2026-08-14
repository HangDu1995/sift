# tests/test_audit_by_skill.py
"""audit --kind skill CLI 接线测试。

mock TranscriptAuditor(不真跑 LLM/判定),验证:
- ``--kind skill`` 把多 skill transcript 按 target skill 切段 → 每段写 jsonl → 喂 auditor
- skill 被调 N 次 → auditor 收到 N 个切片路径
- 切片只含 target skill 段记录(他 skill 的记录不混入)
- target 从未被调用 → exit 2 + 友好错误
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


# ---- 合成 transcript 辅助(仿 test_slice.py / test_audit_integration.py)---------


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _skill(tid: str, name: str) -> dict:
    """Skill tool_use 记录(主作用域)。"""
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Skill", "input": {"skill": name}}]}}


def _tool_use(tid: str, name: str, inp: dict) -> dict:
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _asst_text(text: str) -> dict:
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


# ---- skill 目录 / fake auditor -----------------------------------------------


def _skill_dir(tmp_path: Path, name: str = "skillA") -> Path:
    sp = tmp_path / name
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n# body\n",
        encoding="utf-8",
    )
    return sp


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="skillA",
        transcripts=["slice.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="做 X",
                source="CONSTRAINT",
                verdict=AuditVerdict.PASS,
                category=AuditCategory.PROHIBITION,
                evidence="未违反",
                method=AuditMethod.LLM,
                transcript="slice.jsonl",
            ),
        ],
        warnings=[],
    )


class _FakeAuditor:
    """记录入参(尤其 transcripts 列表),返回构造报告(不真跑 LLM/判定)。"""

    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600, on_progress=None, refine_instructions=False, generate_headline=False, locate_turns=False):
        self.calls.append(
            {
                "skill_name": skill_name,
                "skill_body": skill_body,
                "transcripts": list(transcripts),
                "model": model,
            }
        )
        return _fake_report()


# ---- 测试 ---------------------------------------------------------------------


def test_by_skill_slices_and_feeds_auditor(monkeypatch, tmp_path):
    """--kind skill: skillA 被调 2 次 → auditor 收到 2 个切片,切片只含 A 段记录。

    transcript 里 skillA 调 2 次(各跟一条 Read A 文件)、skillB 调 1 次(跟 Read B 文件)。
    断言:auditor 收到 2 个切片路径;切片文件在磁盘上;每个切片含 A 的文件路径、不含 B 的。
    """
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path, "skillA")
    records = [
        _skill("s1", "skillA"),
        _tool_use("r1", "Read", {"file_path": "/tmp/A1.md"}),
        _skill("s2", "skillB"),
        _tool_use("r2", "Read", {"file_path": "/tmp/B.md"}),
        _skill("s3", "skillA"),
        _tool_use("r3", "Read", {"file_path": "/tmp/A2.md"}),
    ]
    tp = _write_jsonl(tmp_path / "session.jsonl", records)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "--kind", "skill", "-o", str(out)],
    )
    assert r.exit_code == 0, r.output

    # auditor 收到 2 个切片路径(skillA 被调 2 次 → 2 段)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["skill_name"] == "skillA"
    slices = call["transcripts"]
    assert len(slices) == 2

    # 切片文件确实在磁盘上
    for sp_path in slices:
        assert Path(sp_path).exists()

    # 每个切片只含 skillA 段:A 的文件路径在、B 的不在、skillB 调用记录不在
    all_content = ""
    for sp_path in slices:
        all_content += Path(sp_path).read_text(encoding="utf-8") + "\n"
    assert "/tmp/A1.md" in all_content
    assert "/tmp/A2.md" in all_content
    assert "/tmp/B.md" not in all_content
    assert "skillB" not in all_content

    # 切片写在 slices/ 子目录下(不污染 output 顶层)
    slice_dir = out / "slices"
    assert slice_dir.is_dir()
    assert len(list(slice_dir.glob("*.jsonl"))) == 2


def test_by_skill_never_invoked_exits_2(monkeypatch, tmp_path):
    """--kind skill 但 target skill 从未被调用 → exit 2 + 友好错误(不喂 auditor)。"""
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path, "skillA")
    # transcript 里只调了 skillB,没调 skillA
    records = [
        _skill("s1", "skillB"),
        _tool_use("r1", "Read", {"file_path": "/tmp/B.md"}),
    ]
    tp = _write_jsonl(tmp_path / "session.jsonl", records)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "--kind", "skill", "-o", str(out)],
    )
    assert r.exit_code == 2
    # 友好错误信息(走了 console.print,不是裸 traceback)
    assert "skillA" in r.output
    assert "Error" in r.output or "未在此 transcript" in r.output
    # auditor 没被调(无可对账段,提前退出)
    assert len(fake.calls) == 0


def test_by_skill_off_by_default(monkeypatch, tmp_path):
    """不带 --kind:auditor 直接收到原始 transcript 路径(不切片)。"""
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path, "skillA")
    records = [
        _skill("s1", "skillA"),
        _tool_use("r1", "Read", {"file_path": "/tmp/A1.md"}),
        _skill("s2", "skillB"),
        _tool_use("r2", "Read", {"file_path": "/tmp/B.md"}),
    ]
    tp = _write_jsonl(tmp_path / "session.jsonl", records)
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp), "-o", str(out)],
    )
    assert r.exit_code == 0, r.output

    # 不切片:auditor 收到原始 transcript 路径
    assert fake.calls[0]["transcripts"] == [tp]
    # 没生成 slices 目录
    assert not (out / "slices").exists()


def test_by_skill_multi_transcript_same_stem_no_collision(monkeypatch, tmp_path):
    """多个 --transcript 共享同名 stem(如 run-1/transcript.jsonl + run-2/transcript.jsonl)
    → 切片文件名必须跨 transcript 唯一,否则后一个覆盖前一个(静默丢数据、后者被对账两次)。

    回归保证:ti 外层序号入文件名后,两个同名 stem 的 transcript 各产出独立切片、内容互不丢失。
    """
    fake = _FakeAuditor()
    monkeypatch.setattr(
        "sift.cli.commands.audit.TranscriptAuditor", lambda: fake
    )

    sp = _skill_dir(tmp_path, "skillA")

    # 两个 transcript 同名 stem "transcript"、父目录不同;内容各异(不同的 A 文件路径)
    run1 = tmp_path / "run-1"
    run1.mkdir()
    tp1 = _write_jsonl(run1 / "transcript.jsonl", [
        _skill("s1", "skillA"),
        _tool_use("r1", "Read", {"file_path": "/tmp/A-run1.md"}),
    ])
    run2 = tmp_path / "run-2"
    run2.mkdir()
    tp2 = _write_jsonl(run2 / "transcript.jsonl", [
        _skill("s2", "skillA"),
        _tool_use("r2", "Read", {"file_path": "/tmp/A-run2.md"}),
    ])
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp),
         "--transcript", str(tp1),
         "--transcript", str(tp2),
         "--kind", "skill", "-o", str(out)],
    )
    assert r.exit_code == 0, r.output

    # auditor 收到 2 个**互异**切片路径(collision 时会变成 2× 同一路径)
    assert len(fake.calls) == 1
    slices = fake.calls[0]["transcripts"]
    assert len(slices) == 2
    assert len(set(Path(s).name for s in slices)) == 2, (
        f"slice 文件名碰撞(应跨 transcript 唯一): {slices}"
    )

    # 两个切片文件都在盘上,且各自含自己的内容(无丢失)
    contents = {Path(s).name: Path(s).read_text(encoding="utf-8") for s in slices}
    assert len(contents) == 2
    joined = "\n".join(contents.values())
    assert "/tmp/A-run1.md" in joined
    assert "/tmp/A-run2.md" in joined


def test_by_instruction_split_into_fail_and_na_tables(monkeypatch, tmp_path):
    """聚合表拆两张:fail>0 进「违规」表(fail%/fail/total),na>0 进「场景不适用」表
    (na/total + 哪几段不适用);全 PASS 隐藏。na-only 行不再套 fail 列(那曾误导:
    N/A 行显示 0% fail / fail/total 0/2,看着像有 fail)。

    造 2-transcript 报告:I-FAIL(t1 FAIL/t2 PASS)、I-NA(两段都 N/A)、I-PASS(全 PASS)。
    断言:I-FAIL 在违规表带 1/2;I-NA 在 N/A 表带 2/2 + 两段;I-PASS 不出现。
    """
    from sift.audit.aggregate import aggregate_by_instruction

    findings = [
        AuditFinding(instruction_text="I-FAIL", source="HEADING_STEP",
                     verdict=AuditVerdict.FAIL, category=AuditCategory.STEP,
                     evidence="e", method=AuditMethod.LLM, transcript="t1.jsonl", seq=2),
        AuditFinding(instruction_text="I-FAIL", source="HEADING_STEP",
                     verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                     evidence="e", method=AuditMethod.LLM, transcript="t2.jsonl", seq=1),
        AuditFinding(instruction_text="I-NA", source="REFERENCE_USAGE",
                     verdict=AuditVerdict.NA, category=AuditCategory.CONDITIONAL,
                     evidence="e", method=AuditMethod.LLM, transcript="t1.jsonl", seq=3),
        AuditFinding(instruction_text="I-NA", source="REFERENCE_USAGE",
                     verdict=AuditVerdict.NA, category=AuditCategory.CONDITIONAL,
                     evidence="e", method=AuditMethod.LLM, transcript="t2.jsonl", seq=4),
        AuditFinding(instruction_text="I-PASS", source="HEADING_STEP",
                     verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                     evidence="e", method=AuditMethod.LLM, transcript="t1.jsonl", seq=5),
        AuditFinding(instruction_text="I-PASS", source="HEADING_STEP",
                     verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                     evidence="e", method=AuditMethod.LLM, transcript="t2.jsonl", seq=6),
    ]
    report = AuditReport(
        skill_name="skillA",
        transcripts=["t1.jsonl", "t2.jsonl"],
        summary={"total": 6, "pass": 3, "fail": 1, "na": 2},
        findings=findings,
        warnings=[],
        by_instruction=aggregate_by_instruction(findings),
    )

    class _Fake:
        def audit(self, *a, **k):
            return report

    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: _Fake())

    sp = _skill_dir(tmp_path, "skillA")
    tp1 = _write_jsonl(tmp_path / "t1.jsonl", [_skill("s1", "skillA")])
    tp2 = _write_jsonl(tmp_path / "t2.jsonl", [_skill("s2", "skillA")])
    out = tmp_path / "out"

    r = runner.invoke(
        app,
        ["audit", str(sp), "--transcript", str(tp1), "--transcript", str(tp2),
         "-o", str(out)],
    )
    assert r.exit_code == 0, r.output
    text = r.output

    # 违规表:I-FAIL + fail/total 1/2 + 明细 #2(只有 t1 那条 FAIL 的 seq;t2 是 PASS 不进 fail_seqs)
    assert "I-FAIL" in text
    assert "1/2" in text
    assert "#2" in text
    # 场景不适用表:I-NA + na/total 2/2 + 明细 #3,#4(两条 N/A 的 seq),且两段都列出
    assert "I-NA" in text
    assert "2/2" in text
    assert "#3" in text and "#4" in text
    assert "t1.jsonl" in text and "t2.jsonl" in text
    # 全 PASS 的 I-PASS 隐藏
    assert "I-PASS" not in text
