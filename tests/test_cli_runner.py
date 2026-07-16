"""Tests for Claude CLI runner: prompt construction + stream-json token parsing."""

from __future__ import annotations

import json
import os
import stat

import pytest

from skill_eval.errors import ClaudeRunnerError
from skill_eval.execution.cli_runner import ClaudeRunner, _build_non_interactive_prompt


class TestNonInteractivePrompt:
    """P1-4: non_interactive prompt 不诱导捏造,与 skill 的反捏造指令不冲突。"""

    def test_does_not_say_generate_synthetic_data(self):
        p = _build_non_interactive_prompt("do task", "myskill", "body content")
        assert "generate representative synthetic test data" not in p
        assert "do NOT fabricate" in p
        assert "invent data" in p

    def test_preserves_skill_body_and_user_prompt(self):
        p = _build_non_interactive_prompt("THE USER TASK", "myskill", "SKILL BODY HERE")
        assert "<skill name=\"myskill\">" in p
        assert "SKILL BODY HERE" in p
        assert "THE USER TASK" in p
        assert "<non_interactive_mode>" in p

    def test_still_requires_non_interactive_progress(self):
        """P1-4 不应削弱 non_interactive 语义 —— 仍要求跑完全流程、不卡住。"""
        p = _build_non_interactive_prompt("task", "s", "b")
        assert "DO NOT skip steps" in p
        assert "NEVER output questions" in p

    def test_real_data_first_when_missing(self):
        """缺数据时的正确行为:报告缺失 + 用可得数据继续,而非捏造。"""
        p = _build_non_interactive_prompt("task", "s", "b")
        assert "use real files" in p
        assert "state what is missing" in p
        assert "proceed with what is available" in p


class TestProgressFromEvent:
    """stream-json 一行 → 进度字符串(供 run 流式 on_progress 实时吐)。
    tool_use 是动作信号(Read/Bash/Write…),result 是完成信号;assistant 散文太吵不报。"""

    def test_tool_use_file_path(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "audit/aggregate.py"}}]}})
        assert _progress_from_event(line) == "🔧 Read: audit/aggregate.py"

    def test_tool_use_command(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "grep -n x audit/aggregate.py"}}]}})
        assert _progress_from_event(line) == "🔧 Bash: grep -n x audit/aggregate.py"

    def test_result_event_completion(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        line = json.dumps({"type": "result", "subtype": "success", "num_turns": 5,
                           "stop_reason": "end_turn", "usage": {"input_tokens": 12000, "output_tokens": 345}})
        assert _progress_from_event(line) == "✓ 完成: end_turn, 5 轮, 12345 tok"

    def test_assistant_text_not_reported(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "let me analyze the file"}]}})
        assert _progress_from_event(line) is None

    def test_garbage_and_empty_return_none(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        assert _progress_from_event("") is None
        assert _progress_from_event("not json") is None

    def test_long_input_truncated(self):
        from skill_eval.execution.cli_runner import _progress_from_event
        long_cmd = "x" * 200
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": long_cmd}}]}})
        out = _progress_from_event(line)
        assert out.startswith("🔧 Bash: ")
        assert len(out) < 100 and out.endswith("…")
    """result event token 解析:claude CLI 2.x 顶层无 total_tokens,token 在 usage 子对象
    (input_tokens + output_tokens,标准 Anthropic 字段)。GLM/Claude 原生 2.x 实测同结构。"""

    def test_token_from_usage_when_no_top_level_total_tokens(self):
        """claude CLI 2.x 真实结构:result event 无顶层 total_tokens,token 在 usage。"""
        stdout = json.dumps({
            "type": "result", "subtype": "success",
            "result": "pong", "num_turns": 1, "stop_reason": "end_turn",
            "usage": {"input_tokens": 12239, "output_tokens": 3,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 12672},
        })
        runner = ClaudeRunner()
        _, total_tokens, _, result_text, num_turns, stop_reason, _ = runner._parse_stream_json(stdout)
        assert total_tokens == 12242  # 12239 + 3(不含 cache)
        assert result_text == "pong"
        assert num_turns == 1
        assert stop_reason == "end_turn"

    def test_top_level_total_tokens_still_preferred(self):
        """老格式兼容:result 有顶层 total_tokens 时优先用,不回退 usage。"""
        stdout = json.dumps({
            "type": "result", "total_tokens": 9999,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "result": "x", "num_turns": 1,
        })
        runner = ClaudeRunner()
        _, total_tokens, _, _, _, _, _ = runner._parse_stream_json(stdout)
        assert total_tokens == 9999

    def test_zero_tokens_when_neither_present(self):
        """两者都没有 → 0(不崩,兼容 result event 极端缺字段)。"""
        stdout = json.dumps({"type": "result", "result": "x", "num_turns": 1})
        runner = ClaudeRunner()
        _, total_tokens, _, _, _, _, _ = runner._parse_stream_json(stdout)
        assert total_tokens == 0


class TestSpawnIdentity:
    """终端跑 run --task:ClaudeRunner spawn 的 claude 子进程必须带 claude-code 身份
    (AI_AGENT + CLAUDE_CODE_ENTRYPOINT),否则后端过载时撞 529(同 call_claude,见
    utils.claude_cli_env)。用户终端默认没这俩变量,故缺则补 —— 与 call_claude 共用
    claude_cli_env helper,防漂移。"""

    def test_run_injects_identity_when_absent(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock
        import skill_eval.execution.cli_runner as mod
        monkeypatch.delenv("AI_AGENT", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
        proc = MagicMock()
        # 流式 run 读 proc.stdout.readline:mock 成立即 EOF,reader 线程马上收尾
        proc.stdout.readline.return_value = b""
        proc.stderr.read.return_value = b""
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen = MagicMock(return_value=proc)
        monkeypatch.setattr(mod.subprocess, "Popen", mock_popen)
        # 不传 skill_path:避开 parse_skill_md;cwd=tmp_path 钉 project_root
        ClaudeRunner().run(prompt="do task", cwd=tmp_path)
        _, kwargs = mock_popen.call_args
        env = kwargs["env"]
        assert env.get("AI_AGENT") == "claude-code"
        assert env.get("CLAUDE_CODE_ENTRYPOINT") == "claude-vscode"


class TestRunStreaming:
    """run() 流式读 + on_progress:cc 逐行出 stream-json,边读边吐进度,不再全程静默到结束。
    用 fake claude(PATH 注入)覆盖真流式路径 + 超时。"""

    def _fake_claude(self, tmp_path, body):
        p = tmp_path / "claude"
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _prepend(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    def test_emits_progress_and_parses_result(self, monkeypatch, tmp_path):
        body = (
            """printf '%s\\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"audit/aggregate.py"}}]}}'\n"""
            """printf '%s\\n' '{"type":"result","subtype":"success","num_turns":1,"stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":5},"result":"done"}'\n"""
        )
        self._fake_claude(tmp_path, body)
        self._prepend(monkeypatch, tmp_path)
        collected = []
        out = ClaudeRunner().run(prompt="x", cwd=tmp_path, on_progress=collected.append)
        # 流式吐了 tool_use + 完成
        assert "🔧 Read: audit/aggregate.py" in collected
        assert any(m.startswith("✓ 完成") for m in collected)
        # 最终解析照常
        assert out.result_text == "done"
        assert out.tool_calls.get("Read") == 1

    def test_timeout_no_output_raises(self, monkeypatch, tmp_path):
        self._fake_claude(tmp_path, "exec sleep 30\n")
        self._prepend(monkeypatch, tmp_path)
        with pytest.raises(ClaudeRunnerError, match="timed out"):
            ClaudeRunner().run(prompt="x", cwd=tmp_path, timeout=2)
