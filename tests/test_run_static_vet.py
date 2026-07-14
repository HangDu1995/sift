"""run 末尾自动产静态评分卡 + 安全审查(供 report 面板自动带上)。

设计:run 是评测主命令,产物该产齐所有维度。analyze/vet 秒级零 API 成本、纯本地,
末尾追加它们让后续 `report` 的"评分卡/安全"面板自动有数(不再默认空)。
失败不阻断 run(它们是补充维度,挂了只让 report 缺那块,不影响动态评测)。

测的是 `_emit_static_and_vet` helper 本身(直接调引擎,不 spawn cc),
不是 CLI 端到端(端到端会跑真 claude,留给手动验证)。
"""

from __future__ import annotations

import json
from pathlib import Path

from skill_eval.cli.commands.run import _emit_static_and_vet


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Creates and validates data scripts for users.\n---\n\n"
        "## Example\n\nNEVER fabricate data. Based on references/guide.md.\n",
        encoding="utf-8",
    )
    return sp


def test_emit_both_default(tmp_path: Path):
    """默认(都不跳过):scorecard + vet-report 都写进 iteration_dir,内容是合法 JSON。"""
    sp = _skill(tmp_path)
    it = tmp_path / "iteration-1"
    it.mkdir()
    _emit_static_and_vet(sp, it)
    sc = it / "scorecard.json"
    vr = it / "vet-report.json"
    assert sc.exists(), "默认该产 scorecard.json"
    assert vr.exists(), "默认该产 vet-report.json"
    sc_data = json.loads(sc.read_text(encoding="utf-8"))
    vr_data = json.loads(vr.read_text(encoding="utf-8"))
    assert "score" in sc_data and "max_score" in sc_data
    assert "verdict" in vr_data and "scanners" in vr_data


def test_emit_skip_static(tmp_path: Path):
    """skip_static:只产 vet-report,不产 scorecard。"""
    sp = _skill(tmp_path)
    it = tmp_path / "iteration-1"
    it.mkdir()
    _emit_static_and_vet(sp, it, skip_static=True)
    assert not (it / "scorecard.json").exists()
    assert (it / "vet-report.json").exists()


def test_emit_skip_vet(tmp_path: Path):
    """skip_vet:只产 scorecard,不产 vet-report。"""
    sp = _skill(tmp_path)
    it = tmp_path / "iteration-1"
    it.mkdir()
    _emit_static_and_vet(sp, it, skip_vet=True)
    assert (it / "scorecard.json").exists()
    assert not (it / "vet-report.json").exists()


def test_emit_static_failure_nonblocking(tmp_path: Path, monkeypatch):
    """静态分析抛异常:不阻断,不写 scorecard,但 vet-report 照常产(各自独立)。"""
    sp = _skill(tmp_path)
    it = tmp_path / "iteration-1"
    it.mkdir()

    from skill_eval.static_analysis.analyzer import StaticAnalyzer

    def boom(self, skill_path):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(StaticAnalyzer, "analyze", boom)
    _emit_static_and_vet(sp, it)  # 不该抛
    assert not (it / "scorecard.json").exists(), "静态失败时不该写残缺 scorecard"
    assert (it / "vet-report.json").exists(), "vet 该照常产(独立失败)"


def test_emit_vet_failure_nonblocking(tmp_path: Path, monkeypatch):
    """安全审查抛异常:不阻断,不写 vet-report,但 scorecard 照常产。"""
    sp = _skill(tmp_path)
    it = tmp_path / "iteration-1"
    it.mkdir()

    from skill_eval.security.vetter import SecurityVetter

    def boom(self, skill_path):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(SecurityVetter, "vet", boom)
    _emit_static_and_vet(sp, it)  # 不该抛
    assert (it / "scorecard.json").exists(), "scorecard 该照常产(独立失败)"
    assert not (it / "vet-report.json").exists(), "vet 失败时不该写残缺 vet-report"
