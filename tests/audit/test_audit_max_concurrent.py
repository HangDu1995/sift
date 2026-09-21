# tests/test_audit_max_concurrent.py
"""audit 并发配置面测试:--max-concurrent 固定(关 AIMD)/ env / 默认 AIMD 自适应 / env=0 显式关。

照 test_audit_root.py 模式:mock TranscriptAuditor 不真跑 LLM;mock configure_max_concurrent /
configure_adaptive 为 spy 验证接线(不污染模块级限制器)。
"""

from __future__ import annotations

import pytest
from cli_helpers import invoke_cli
from report_fixtures import root_audit_report

from sift import concurrency as audmod
from sift.audit.judge_context import AuditOptions
from sift.models.audit_models import (
    AuditReport,
)

from .helpers import create_root_skill as _skill_dir
from .helpers import write_jsonl as _write_jsonl


def _asst(t):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _fake_report() -> AuditReport:
    return root_audit_report("do X")


class _FakeAuditor:
    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, options: AuditOptions = AuditOptions()):
        self.calls.append({"transcripts": list(transcripts)})
        return _fake_report()


def _setup(monkeypatch):
    """mock TranscriptAuditor + configure_max_concurrent/configure_adaptive(spy)。
    返回 ``(fake, mc_calls, adapt_calls)``。
    """
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)
    mc_calls: list[int] = []
    adapt_calls: list[int] = []
    monkeypatch.setattr("sift.cli.commands.audit.configure_max_concurrent", lambda n: mc_calls.append(n))
    monkeypatch.setattr("sift.cli.commands.audit.configure_adaptive", lambda *a, **k: adapt_calls.append(1))
    return fake, mc_calls, adapt_calls


def _tp(tmp_path):
    return _write_jsonl(tmp_path / "s.jsonl", [_asst("hi")])


# ---- 限制器单元测(模块级限制器真改,finally 还原)----


def test_configure_max_concurrent_resizes_limiter():
    """configure(n) → 限制器固定 ceiling=n;第 n+1 个非阻塞 acquire 失败。"""
    try:
        audmod.configure_max_concurrent(3)
        lim = audmod.LLM_SEMAPHORE
        assert lim.acquire(blocking=False)  # 1
        assert lim.acquire(blocking=False)  # 2
        assert lim.acquire(blocking=False)  # 3
        assert not lim.acquire(blocking=False)  # 4 → 满
        lim.release()
        lim.release()
        lim.release()
    finally:
        audmod.configure_adaptive(audmod.MAX_BATCH_CONCURRENT)  # 还原自适应默认


def test_configure_max_concurrent_rejects_invalid():
    with pytest.raises(ValueError):
        audmod.configure_max_concurrent(0)
    with pytest.raises(ValueError):
        audmod.configure_max_concurrent(-1)


# ---- CLI / env 接线测(spy configure_*,不真改限制器)----


def test_audit_max_concurrent_cli(monkeypatch, tmp_path):
    """--max-concurrent 3 → configure_max_concurrent(3) 被调、adaptive 不调(固定模式)。"""
    _, mc, adapt = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "--max-concurrent", "3", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert mc == [3]
    assert adapt == []


def test_audit_max_concurrent_env(monkeypatch, tmp_path):
    """无 CLI 但 env SIFT_MAX_CONCURRENT=4 → configure_max_concurrent(4)。"""
    _, mc, adapt = _setup(monkeypatch)
    monkeypatch.setenv("SIFT_MAX_CONCURRENT", "4")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert mc == [4]
    assert adapt == []


def test_audit_default_is_adaptive(monkeypatch, tmp_path):
    """无 CLI 无 env → 默认 AIMD 自适应(configure_adaptive 被调、configure_max_concurrent 不调)。"""
    _, mc, adapt = _setup(monkeypatch)
    monkeypatch.delenv("SIFT_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("SIFT_ADAPTIVE_CONCURRENCY", raising=False)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert adapt == [1]
    assert mc == []


def test_audit_adaptive_env_zero_falls_back_fixed(monkeypatch, tmp_path):
    """env SIFT_ADAPTIVE_CONCURRENCY=0 → 显式关自适应,configure_max_concurrent(默认) 被调。"""
    _, mc, adapt = _setup(monkeypatch)
    monkeypatch.delenv("SIFT_MAX_CONCURRENT", raising=False)
    monkeypatch.setenv("SIFT_ADAPTIVE_CONCURRENCY", "0")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert mc == [audmod.MAX_BATCH_CONCURRENT]
    assert adapt == []


def test_audit_max_concurrent_cli_overrides_env(monkeypatch, tmp_path):
    """CLI --max-concurrent 3 + env=4 → CLI 赢(configure_max_concurrent(3))。"""
    _, mc, adapt = _setup(monkeypatch)
    monkeypatch.setenv("SIFT_MAX_CONCURRENT", "4")
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "--max-concurrent", "3", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert mc == [3]
    assert adapt == []


def test_audit_max_concurrent_invalid_exits_2(monkeypatch, tmp_path):
    """--max-concurrent 0 → exit 2(不调 configure,不喂 auditor)。"""
    fake, mc, adapt = _setup(monkeypatch)
    sp = _skill_dir(tmp_path)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(_tp(tmp_path)), "--max-concurrent", "0", "-o", str(out)])
    assert r.exit_code == 2
    assert "max-concurrent" in r.output.lower()
    assert mc == []
    assert adapt == []
    assert len(fake.calls) == 0
