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
