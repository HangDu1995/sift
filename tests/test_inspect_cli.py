"""inspect 命令(trigger_probes 显示)的 CLI 测试。"""
import json

from typer.testing import CliRunner

from skill_eval.cli.main import app
from skill_eval.models import EvalCase, EvalSet, TriggerProbe


def _write(es: EvalSet, path):
    path.write_text(es.model_dump_json(), encoding="utf-8")
    return path


def test_inspect_shows_trigger_probes_breakdown(tmp_path):
    es = EvalSet(
        skill_name="sk",
        evals=[EvalCase(id=1, prompt="p", expectations=["e"])],
        trigger_probes=[TriggerProbe(id=1, prompt="q1", expect_trigger=True, rationale=""),
                        TriggerProbe(id=2, prompt="q2", expect_trigger=False, rationale=""),
                        TriggerProbe(id=3, prompt="q3", expect_trigger=True, rationale="")],
    )
    es_path = _write(es, tmp_path / "evals.json")
    r = CliRunner().invoke(app, ["inspect", str(es_path)])
    assert r.exit_code == 0, r.output
    assert "trigger 探题" in r.output
    assert "3 道" in r.output
    assert "should-trigger 2" in r.output   # 2 个 expect_trigger=True
    assert "should-not 1" in r.output       # 1 个 expect_trigger=False


def test_inspect_no_trigger_probes_omits_line(tmp_path):
    """无 trigger_probes → 不出 trigger 行(别出噪声)。"""
    es = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="p", expectations=["e"])])
    es_path = _write(es, tmp_path / "evals.json")
    r = CliRunner().invoke(app, ["inspect", str(es_path)])
    assert r.exit_code == 0, r.output
    assert "trigger 探题" not in r.output
