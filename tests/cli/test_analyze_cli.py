"""analyze CLI:落盘行为(无 -o 不写文件,有 -o 才写)。

防回归:旧版 analyze 无条件写 `<skill>/scorecard.json` 污染被测 skill 目录;
现改为默认只终端输出,显式 -o 才落盘(对齐 run/audit 的"只读 skill、报告外置")。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sift.cli.main import app

runner = CliRunner()


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Creates and validates data scripts for users.\n"
        "---\n\n## Example\n\n```python\nprint('hi')\n```\n\n"
        "NEVER fabricate data. Based on references/guide.md.\n",
        encoding="utf-8",
    )
    return sp


def test_analyze_no_output_does_not_pollute_skill_dir(tmp_path: Path):
    """无 -o:只终端输出,不往 skill 目录写 scorecard.json(防回归到旧污染行为)。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["analyze", str(sp)])
    assert r.exit_code in (0, 1), r.output  # score 可能不满 → exit 1,但落盘行为与 exit 无关
    assert not (sp / "scorecard.json").exists(), "默认不应往 skill 目录写 scorecard.json"


def test_analyze_with_output_writes_there(tmp_path: Path):
    """有 -o:写到指定文件,且不污染 skill 目录。"""
    sp = _skill(tmp_path)
    out = tmp_path / "out" / "scorecard.json"
    r = runner.invoke(app, ["analyze", str(sp), "-o", str(out)])
    assert r.exit_code in (0, 1), r.output
    assert out.exists()
    assert not (sp / "scorecard.json").exists(), "-o 时也不该往 skill 目录写"


def test_analyze_json_no_output_prints_only(tmp_path: Path):
    """--format json 无 -o:打印 JSON 到 stdout,不落盘。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["analyze", str(sp), "--format", "json"])
    assert r.exit_code in (0, 1), r.output
    assert not (sp / "scorecard.json").exists()
