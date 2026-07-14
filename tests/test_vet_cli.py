"""vet CLI:落盘行为(无 -o 不写文件,有 -o 才写)。

防回归:旧版 vet 无条件写 `<skill>/vet-report.json` 污染被测 skill 目录;
现改为默认只终端输出,显式 -o 才落盘(对齐 run/audit)。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skill_eval.cli.main import app

runner = CliRunner()


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Creates and validates data scripts for users.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return sp


def test_vet_no_output_does_not_pollute_skill_dir(tmp_path: Path):
    """无 -o:只终端输出,不往 skill 目录写 vet-report.json。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["vet", str(sp), "--skip-external"])
    assert r.exit_code == 0, r.output  # 干净 skill → SAFE
    assert not (sp / "vet-report.json").exists(), "默认不应往 skill 目录写 vet-report.json"


def test_vet_with_output_writes_there(tmp_path: Path):
    """有 -o:写到指定文件,且不污染 skill 目录。"""
    sp = _skill(tmp_path)
    out = tmp_path / "v.json"
    r = runner.invoke(app, ["vet", str(sp), "--skip-external", "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert out.exists()
    assert not (sp / "vet-report.json").exists(), "-o 时也不该往 skill 目录写"
