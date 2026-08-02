"""add 命令 CLI 测试(--idea LLM 路径的 token/耗时显示)。"""
import json

from typer.testing import CliRunner

from skill_eval.cli.main import app

runner = CliRunner()


def _skill_with_evals(tmp_path):
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
    edir = sp / "evals"
    edir.mkdir()
    (edir / "evals.json").write_text(json.dumps({"skill_name": "s", "evals": [
        {"id": 1, "prompt": "old", "level": "L1", "expectations": ["e"], "assertions": []}]}),
        encoding="utf-8")
    return sp


def test_add_idea_prints_token_and_duration(monkeypatch, tmp_path):
    """add --idea 走 LLM → 显示 token/耗时(和 init 一致;add 之前漏了)。"""
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.add.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.cli.commands.add._check_claude_or_exit", lambda: None)
    fake = json.dumps({"name": "n", "prompt": "new q", "expectations": ["e"],
                       "assertions": [{"name": "a", "type": "CONTAINS_PATTERN", "params": {"pattern": "k"}}]})

    def fake_call(prompt, *a, **k):
        on_usage = k.get("on_usage")
        if on_usage:
            on_usage({"input_tokens": 150, "output_tokens": 50})
        return fake

    monkeypatch.setattr("skill_eval.cli.commands.add.call_claude", fake_call)
    sp = _skill_with_evals(tmp_path)
    r = runner.invoke(app, ["add", str(sp), "--idea", "test something"], input="y\n")
    assert r.exit_code == 0, r.output
    assert any("耗时" in p and "tok" in p for p in printed), printed
    assert any("200" in p for p in printed), printed  # 150 + 50
