# tests/test_audit_max_concurrent.py
"""audit --max-concurrent CLI + env SKILL_EVAL_MAX_CONCURRENT 测试。

LLM 判定并发上限可配(CLI > env > 默认 6),给大声明 / 低内存 / 多 audit 共存场景
一个安全旋钮(用时间换内存,防 OOM)。照 test_audit_root.py 模式:mock TranscriptAuditor
不真跑 LLM;另 mock configure_max_concurrent 为 spy 验证接线(不污染模块级信号量)。
"""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from typer.testing import CliRunner
from skill_eval.cli.main import app
from skill_eval.audit import auditor as audmod
from skill_eval.models.audit_models import (
    AuditCategory, AuditFinding, AuditMethod, AuditReport, AuditVerdict,
)

runner = CliRunner()


def _skill_dir(tmp_path: Path, name: str = "wf") -> Path:
    sp = tmp_path / name
    sp.mkdir()
    (sp / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n# body\n", encoding="utf-8")
    return sp


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _asst(t): return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _fake_report() -> AuditReport:
    return AuditReport(
        skill_name="wf", transcripts=["slice.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[AuditFinding(instruction_text="do X", source="HEADING_STEP",
                               verdict=AuditVerdict.PASS, category=AuditCategory.STEP,
                               evidence="ok", method=AuditMethod.LLM, transcript="slice.jsonl")],
        warnings=[],
    )


class _FakeAuditor:
    def __init__(self): self.calls: list[dict] = []
    def audit(self, skill_name, skill_body, transcripts, model=None, timeout=600,
              on_progress=None, refine_instructions=False, locate_turns=False):
        self.calls.append({"transcripts": list(transcripts)})
        return _fake_report()


def _setup(monkeypatch):
    """mock TranscriptAuditor + configure_max_concurrent(spy)。返回 (fake, calls)。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", lambda: fake)
    calls: list[int] = []
    monkeypatch.setattr("skill_eval.cli.commands.audit.configure_max_concurrent",
                        lambda n: calls.append(n))
    return fake, calls


def _tp(tmp_path):
    return _write_jsonl(tmp_path / "s.jsonl", [_asst("hi")])


# ---- configure_max_concurrent 单元测(模块级信号量真改,finally 还原)----

def test_configure_max_concurrent_resizes_semaphore():
    """configure(n) 后信号量恰好放行 n 个,第 n+1 个非阻塞 acquire 失败。"""
    try:
        audmod.configure_max_concurrent(3)
        sem = audmod._LLM_SEMAPHORE
        assert sem.acquire(blocking=False)      # 1
        assert sem.acquire(blocking=False)      # 2
        assert sem.acquire(blocking=False)      # 3
        assert not sem.acquire(blocking=False)  # 4 → 满
        sem.release(); sem.release(); sem.release()
    finally:
        audmod.configure_max_concurrent(audmod._MAX_BATCH_CONCURRENT)  # 还原默认


def test_configure_max_concurrent_rejects_invalid():
    with pytest.raises(ValueError):
        audmod.configure_max_concurrent(0)
    with pytest.raises(ValueError):
        audmod.configure_max_concurrent(-1)


# ---- CLI / env 接线测(spy configure,不真改信号量)----

def test_audit_max_concurrent_cli(monkeypatch, tmp_path):
    """--max-concurrent 3 → configure(3) 被调。"""
    fake, calls = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(_tp(tmp_path)),
                            "--max-concurrent", "3", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert calls == [3]
    assert len(fake.calls) == 1


def test_audit_max_concurrent_env(monkeypatch, tmp_path):
    """无 CLI 但 env SKILL_EVAL_MAX_CONCURRENT=4 → configure(4) 被调。"""
    fake, calls = _setup(monkeypatch)
    monkeypatch.setenv("SKILL_EVAL_MAX_CONCURRENT", "4")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(_tp(tmp_path)), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert calls == [4]


def test_audit_max_concurrent_default_not_called(monkeypatch, tmp_path):
    """无 CLI 无 env → configure 不被调(保持模块默认 6)。"""
    fake, calls = _setup(monkeypatch)
    monkeypatch.delenv("SKILL_EVAL_MAX_CONCURRENT", raising=False)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(_tp(tmp_path)), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert calls == []


def test_audit_max_concurrent_cli_overrides_env(monkeypatch, tmp_path):
    """CLI --max-concurrent 3 + env=4 → CLI 赢(configure(3))。"""
    _, calls = _setup(monkeypatch)
    monkeypatch.setenv("SKILL_EVAL_MAX_CONCURRENT", "4")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(_tp(tmp_path)),
                            "--max-concurrent", "3", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert calls == [3]


def test_audit_max_concurrent_invalid_exits_2(monkeypatch, tmp_path):
    """--max-concurrent 0 → exit 2(不调 configure,不喂 auditor)。"""
    fake, calls = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = runner.invoke(app, ["audit", str(sp), "--transcript", str(_tp(tmp_path)),
                            "--max-concurrent", "0", "-o", str(out)])
    assert r.exit_code == 2
    assert "max-concurrent" in r.output.lower()
    assert calls == []
    assert len(fake.calls) == 0
