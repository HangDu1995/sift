import json
def test_trigger_command_runs_probes_and_writes_report(tmp_path, monkeypatch):
    """trigger 命令:读 trigger_probes → TriggerRunner.run(mock)→ 写 trigger-report.json。"""
    from typer.testing import CliRunner
    from skill_eval.cli.main import app

    skill = tmp_path / "skill"; skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# My\nbody\n")
    (skill / "evals").mkdir()
    (skill / "evals" / "evals.json").write_text(
        '{"skill_name":"my-skill","evals":[],'
        '"trigger_probes":[{"id":1,"prompt":"q1","expect_trigger":true,"rationale":""}]}')

    fake = {"probes":[{"id":1,"expect_trigger":True,"prompt":"q1","trigger_rate":1.0,"verdict":"PASS","unresolved":False,"tokens":10,"transcript_path":"evals/probe-1/run-0/transcript.jsonl"}],
            "recall":1.0,"false_trigger_rate":None,"n_should_trigger":1,"n_should_not":0,"unresolved":0}
    monkeypatch.setattr("skill_eval.cli.commands.trigger.TriggerRunner.run", lambda self, probes, base: fake)

    out = tmp_path / "out"
    r = CliRunner().invoke(app, ["trigger", str(skill), "-o", str(out), "--runs", "1"])
    assert r.exit_code == 0, r.output
    rep = json.loads((out / "trigger-report.json").read_text())
    assert rep["skill_name"] == "my-skill"
    assert rep["recall"] == 1.0
    assert len(rep["description_hash"]) == 16


def test_trigger_command_renders_unresolved_probe_without_crash(tmp_path, monkeypatch):
    """UNRESOLVED probe 的 trigger_rate=None 时,控制台摘要不崩({None:.0%} 会 TypeError)、report 正常写。"""
    from typer.testing import CliRunner
    from skill_eval.cli.main import app

    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")
    (skill / "evals").mkdir()
    (skill / "evals" / "evals.json").write_text(
        '{"skill_name":"my-skill","evals":[],'
        '"trigger_probes":[{"id":1,"prompt":"q","expect_trigger":true}]}')

    fake = {"probes": [{"id": 1, "expect_trigger": True, "trigger_rate": None,
                        "verdict": "UNRESOLVED", "unresolved": True, "tokens": 0,
                        "transcript_path": "", "prompt": "q"}],
            "recall": None, "false_trigger_rate": None,
            "n_should_trigger": 0, "n_should_not": 0, "unresolved": 1}
    monkeypatch.setattr("skill_eval.cli.commands.trigger.TriggerRunner.run",
                        lambda self, probes, base: fake)

    out = tmp_path / "out"
    r = CliRunner().invoke(app, ["trigger", str(skill), "-o", str(out)])
    assert r.exit_code == 0, r.output            # 不崩
    rep = json.loads((out / "trigger-report.json").read_text())
    assert rep["probes"][0]["trigger_rate"] is None
    assert rep["probes"][0]["verdict"] == "UNRESOLVED"
