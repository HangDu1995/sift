"""trigger 模式 spawn(run_trigger)单测。

run_trigger 是 trigger-accuracy 评测用的 spawn 路径:不灌 skill body、不加
--disable-slash-commands,让 cc 按 description 自动发现 cwd/.claude/skills/ 下的 skill。
"""

from unittest.mock import patch

from skill_eval.execution.cli_runner import ClaudeRunner


def _make(stream_json_with_skill: bool):
    if stream_json_with_skill:
        return ('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"zorblax-guide"}}]}}\n'
                '{"type":"result","result":"done","usage":{"input_tokens":10,"output_tokens":5}}\n')
    return '{"type":"result","result":"done","usage":{"input_tokens":10,"output_tokens":5}}\n'


def test_run_trigger_builds_cmd_without_inject_or_disable_slash():
    captured = {}

    def fake_spawn(cmd, cwd, env, timeout, on_progress):
        captured["cmd"] = cmd
        captured["env"] = env
        return _make(False), 0, 50, False, ""

    with patch.object(ClaudeRunner, "_spawn", side_effect=fake_spawn):
        ClaudeRunner().run_trigger(prompt="hi", cwd="/tmp/x",
                                   env={"HOME": "/tmp/x", "PATH": "/x"},
                                   model=None, timeout=60, max_turns=5)
    assert "--disable-slash-commands" not in captured["cmd"]   # 关键:不能关 skill
    assert "Follow its instructions" not in " ".join(captured["cmd"])  # 不能灌 body
    assert captured["env"]["HOME"] == "/tmp/x"                 # 隔离 env 透传(调用方拥有隔离)


def test_run_trigger_returns_runoutput_with_skill_in_tool_calls():
    with patch.object(ClaudeRunner, "_spawn", return_value=(_make(True), 0, 50, False, "")):
        out = ClaudeRunner().run_trigger(prompt="hi", cwd="/tmp/x",
                                         env={"HOME": "/tmp/x"}, model=None, timeout=60, max_turns=5)
    assert out.tool_calls.get("Skill") == 1
    assert out.total_tokens == 15


# ---------------------------------------------------------------------------
# Part A — pure functions (probe_verdict, aggregate_rates)
# ---------------------------------------------------------------------------

def test_should_trigger_fired_passes():
    from skill_eval.execution.trigger_runner import probe_verdict
    assert probe_verdict(expect_trigger=True, fired=True) == "PASS"


def test_should_trigger_not_fired_fails():
    from skill_eval.execution.trigger_runner import probe_verdict
    assert probe_verdict(expect_trigger=True, fired=False) == "FAIL"


def test_should_not_fired_passes():
    from skill_eval.execution.trigger_runner import probe_verdict
    assert probe_verdict(expect_trigger=False, fired=False) == "PASS"


def test_should_not_triggered_fails():
    from skill_eval.execution.trigger_runner import probe_verdict
    assert probe_verdict(expect_trigger=False, fired=True) == "FAIL"  # 误触发


def test_aggregate_recall_and_false_trigger():
    from skill_eval.execution.trigger_runner import aggregate_rates
    probes = [
        {"expect_trigger": True, "trigger_rate": 1.0},
        {"expect_trigger": True, "trigger_rate": 0.0},
        {"expect_trigger": True, "trigger_rate": 1.0},
        {"expect_trigger": False, "trigger_rate": 0.0},
        {"expect_trigger": False, "trigger_rate": 0.5},
    ]
    rates = aggregate_rates(probes)
    assert abs(rates["recall"] - 2 / 3) < 1e-6
    assert abs(rates["false_trigger_rate"] - 0.25) < 1e-6
    assert rates["n_should_trigger"] == 3 and rates["n_should_not"] == 2


def test_aggregate_zero_direction_is_na():
    from skill_eval.execution.trigger_runner import aggregate_rates
    rates = aggregate_rates([{"expect_trigger": True, "trigger_rate": 1.0}])  # 无 should-not
    assert rates["false_trigger_rate"] is None
    assert rates["recall"] == 1.0


# ---------------------------------------------------------------------------
# Part B — TriggerRunner integration (run_trigger mocked, not real cc)
# ---------------------------------------------------------------------------

def test_trigger_runner_detects_invocation_end_to_end(monkeypatch, tmp_path):
    """mock run_trigger 返带 Skill tool_use 的 stdout → harness 写 transcript → invoked_skills 检出 fired。"""
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe
    from skill_eval.models.execution_models import RunOutput

    skill_stdout = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"my-skill"}}]}}\n'
        '{"type":"result","result":"x","usage":{"input_tokens":1,"output_tokens":1}}\n'
    )

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        # 关键:fake 只返 RunOutput(stdout=带 Skill 的流)。 transcript 由 harness 用 out.stdout 写,
        # 不要在 fake 里写 transcript(路径不对 + 多余)。
        return RunOutput(exit_code=0, stdout=skill_stdout, tool_calls={"Skill": 1}, total_tokens=2)

    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")

    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=1)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=True)], out_base=tmp_path / "out")
    p1 = res["probes"][0]
    assert p1["trigger_rate"] == 1.0 and p1["verdict"] == "PASS"
    assert res["recall"] == 1.0 and res["n_should_trigger"] == 1


def test_trigger_runner_should_not_triggered_is_fail(monkeypatch, tmp_path):
    """should-not 探题却触发了 → FAIL + 计入 false_trigger_rate。"""
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe
    from skill_eval.models.execution_models import RunOutput

    skill_stdout = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"my-skill"}}]}}\n'
        '{"type":"result","result":"x"}\n'
    )

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        return RunOutput(exit_code=0, stdout=skill_stdout)

    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")

    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=1)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=False)], out_base=tmp_path / "out")
    assert res["probes"][0]["verdict"] == "FAIL"
    assert res["false_trigger_rate"] == 1.0


def test_trigger_runner_spawn_failure_is_unresolved_excluded_from_rates(monkeypatch, tmp_path):
    """run_trigger 抛异常 → probe UNRESOLVED,不进 recall/false_trigger 分母。"""
    from skill_eval.execution.cli_runner import ClaudeRunnerError
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        raise ClaudeRunnerError("boom")

    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")

    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=1)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=True)], out_base=tmp_path / "out")
    assert res["probes"][0]["verdict"] == "UNRESOLVED"
    assert res["unresolved"] == 1
    assert res["recall"] is None  # 唯一 should-trigger 探题 UNRESOLVED → 该方向空 → N/A


def test_trigger_runner_nonzero_exit_without_fire_is_unresolved(monkeypatch, tmp_path):
    """cc 非零退出带 stdout(如限流错误文本)但没触发 → UNRESOLVED,不进分母。

    回归 I1:cc 后端不可用 / 限流时实测 exit 1 + 错误打 stdout、stderr 空,run_trigger
    不 raise 而返正常 RunOutput;若 _run_probe_once 不看 exit_code,会被当「正常没触发」
    → should-trigger 假 FAIL / should-not 假 PASS,静默污染双率。
    """
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe
    from skill_eval.models.execution_models import RunOutput

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        # cc 撞限流:exit 1、stdout 是错误文本(无 Skill tool_use)
        return RunOutput(exit_code=1, stdout="Error: backend overloaded (529)\n", total_tokens=0)

    skill = tmp_path / "my-skill"; skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")
    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=1)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=True)], out_base=tmp_path / "out")
    assert res["probes"][0]["verdict"] == "UNRESOLVED"
    assert res["unresolved"] == 1
    assert res["recall"] is None  # 唯一 should-trigger 探题 UNRESOLVED → N/A,不是假 0.0


def test_trigger_runner_fired_then_nonzero_exit_still_counts(monkeypatch, tmp_path):
    """skill 先触发后 cc 非零退出(fired=True)→ 不计 UNRESOLVED,保住这次 fire(I1 修法的边界)。"""
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe
    from skill_eval.models.execution_models import RunOutput

    skill_stdout = ('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"my-skill"}}]}}\n'
                    '{"type":"result","result":"x"}\n')

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        return RunOutput(exit_code=1, stdout=skill_stdout)  # 触发了,但 cc exit 1

    skill = tmp_path / "my-skill"; skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")
    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=1)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=True)], out_base=tmp_path / "out")
    assert res["probes"][0]["verdict"] == "PASS"   # fired → 不因 exit!=0 丢
    assert res["probes"][0]["trigger_rate"] == 1.0
    assert res["unresolved"] == 0


def test_trigger_runner_multi_run_aggregation(monkeypatch, tmp_path):
    """runs=3:fired 序列 [True,False,True] → trigger_rate=2/3 → should-trigger ≥0.5 → PASS。

    补多 run 聚合接线(fired_count/runs → _binary_verdict)的集成覆盖(单 run 测里聚合是平凡的)。
    """
    from skill_eval.execution.trigger_runner import TriggerRunner
    from skill_eval.models.eval_models import TriggerProbe
    from skill_eval.models.execution_models import RunOutput

    fire_seq = iter([True, False, True])
    skill_stdout_fire = ('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill","input":{"skill":"my-skill"}}]}}\n'
                         '{"type":"result","result":"x"}\n')
    skill_stdout_nofire = '{"type":"result","result":"x"}\n'

    def fake_run_trigger(self, prompt, cwd, env, **kw):
        return RunOutput(exit_code=0,
                         stdout=skill_stdout_fire if next(fire_seq) else skill_stdout_nofire)

    skill = tmp_path / "my-skill"; skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: does X\n---\n# body\n")
    monkeypatch.setattr("skill_eval.execution.trigger_runner.ClaudeRunner.run_trigger", fake_run_trigger)
    tr = TriggerRunner(skill, runs=3)
    res = tr.run([TriggerProbe(id=1, prompt="q", expect_trigger=True)], out_base=tmp_path / "out")
    p1 = res["probes"][0]
    assert abs(p1["trigger_rate"] - 2 / 3) < 1e-6
    assert p1["verdict"] == "PASS"   # 2/3 ≥ 0.5
    assert abs(res["recall"] - 2 / 3) < 1e-6
