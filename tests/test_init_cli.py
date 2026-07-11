# tests/test_init_cli.py
from typer.testing import CliRunner
from skill_eval.cli.main import app
from skill_eval.models import EvalSet, EvalCase

runner = CliRunner()

def _fake_evalset():
    return EvalSet(skill_name="s", evals=[EvalCase(id=1, name="n", prompt="p", level="L1")])

def _skill_dir(tmp_path):
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
    return sp

def test_init_without_flag_calls_generate_with_llm(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: called.__setitem__("default", True) or _fake_evalset())
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp: called.__setitem__("transcript", True) or _fake_evalset())
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("default") is True and "transcript" not in called

def test_init_with_from_transcript_routes(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp: called.__setitem__("tp", tp) or _fake_evalset())
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: called.__setitem__("default", True) or _fake_evalset())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("tp") == tp and "default" not in called


def _fake_evalset_degraded():
    return EvalSet(skill_name="s", evals=[
        EvalCase(id=1, name="transcript-turn-1", prompt="p", level="L1", flags=["degraded:no-llm"])
    ])


def test_init_warns_on_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp: _fake_evalset_degraded())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("no-llm" in p for p in printed), printed


def test_init_no_warning_without_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp: _fake_evalset())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("no-llm" in p for p in printed), printed
