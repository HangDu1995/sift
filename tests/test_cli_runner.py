"""Tests for Claude CLI runner: prompt construction + stream-json token parsing."""

from __future__ import annotations

import json
import os
import stat

import pytest

from skill_eval.errors import ClaudeRunnerError
from skill_eval.execution.cli_runner import (
    ClaudeRunner,
    _build_non_interactive_prompt,
    build_live_prompt,
)


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


class TestSpawnDirect:
    """直接测 ``_spawn`` 的元组契约 ``(stdout, returncode, duration_ms, timed_out, stderr)``。

    锁定 ``run()`` 与将来的 ``run_trigger()``(Task 3,不注入 skill body / 不关 slash)
    共用的 spawn 机制契约。``run()`` 的 e2e 覆盖见 ``TestRunStreaming``(真 fake claude
    走 PATH);这里用 mock Popen 直接断言 ``_spawn`` 本身 —— 快、确定性地覆盖三条路:
    正常完成(含进度回调)/ 超时带部分输出(kill 被调,stdout 保留)/ 超时无输出。
    """

    def _make_proc(self, stdout_lines, stderr=b"", returncode=0):
        """造 mock proc:``readline`` 逐行吐 ``stdout_lines`` 然后立即 EOF(b\"\")。"""
        from unittest.mock import MagicMock
        proc = MagicMock()
        proc.stdout.readline.side_effect = list(stdout_lines) + [b""]
        proc.stderr.read.return_value = stderr
        proc.returncode = returncode
        proc.wait.return_value = returncode
        return proc

    def test_normal_completion_returns_tuple(self, monkeypatch, tmp_path):
        """正常完成:stdout 全收集、returncode 回传、timed_out=False、不 kill、stderr 透传。"""
        from unittest.mock import MagicMock
        import skill_eval.execution.cli_runner as mod

        proc = self._make_proc(
            [
                b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"x.py"}}]}}\n',
                b'{"type":"result","result":"done","usage":{"input_tokens":10,"output_tokens":5}}\n',
            ],
            stderr=b"some warn",
        )
        monkeypatch.setattr(mod.subprocess, "Popen", MagicMock(return_value=proc))

        stdout, returncode, duration_ms, timed_out, stderr_str = (
            ClaudeRunner()._spawn(["claude"], tmp_path, {}, timeout=10, on_progress=None)
        )

        assert timed_out is False
        assert returncode == 0
        assert isinstance(duration_ms, int) and duration_ms >= 0  # 实测毫秒(快测可能 ~0)
        assert '"tool_use"' in stdout and '"result":"done"' in stdout
        assert stderr_str == "some warn"
        proc.kill.assert_not_called()  # 正常完成不 kill
        proc.wait.assert_called_once()

    def test_progress_callback_invoked_per_streamed_line(self, monkeypatch, tmp_path):
        """on_progress:每条 tool_use/result 事件行经 _progress_from_event 吐一条进度。"""
        from unittest.mock import MagicMock
        import skill_eval.execution.cli_runner as mod

        proc = self._make_proc(
            [b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}\n'],
        )
        monkeypatch.setattr(mod.subprocess, "Popen", MagicMock(return_value=proc))

        collected = []
        ClaudeRunner()._spawn(["claude"], tmp_path, {}, timeout=10, on_progress=collected.append)

        assert collected == ["🔧 Read: a.py"]  # 一行一进度(散文行不报)

    def test_timeout_with_partial_output(self, monkeypatch, tmp_path):
        """超时但已有部分输出:timed_out=True、kill 被调一次、已读 stdout 保留、returncode 透传。"""
        import threading
        from unittest.mock import MagicMock
        import skill_eval.execution.cli_runner as mod

        proc = MagicMock()
        # 第一行立刻吐,之后 readline 阻塞(模拟 cc 卡住,主线程 queue.get 会超时)
        block = threading.Event()
        calls = iter([
            b'{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"x"}}]}}\n'
        ])

        def readline():
            try:
                return next(calls)
            except StopIteration:
                block.wait(30)  # reader 线程卡这,主线程 queue.get(timeout) 超时
                return b""

        proc.stdout.readline.side_effect = readline
        proc.stderr.read.return_value = b""
        proc.returncode = -9
        proc.wait.return_value = -9
        monkeypatch.setattr(mod.subprocess, "Popen", MagicMock(return_value=proc))

        stdout, returncode, duration_ms, timed_out, stderr_str = (
            ClaudeRunner()._spawn(["claude"], tmp_path, {}, timeout=1, on_progress=None)
        )

        assert timed_out is True
        assert '"tool_use"' in stdout  # 部分输出保留(run() 据此决定 partial return vs raise)
        assert returncode == -9
        assert stderr_str == ""
        proc.kill.assert_called_once()  # 超时 kill 整个 proc
        block.set()  # 解放 reader daemon 线程,让它撞 EOF 哨兵干净退出


class TestBuildLivePrompt:
    """build_live_prompt:抽自 ClaudeRunner.run 内联块,供 cc/opencode 两执行器共用同一份
    注入 prompt。``skill_path`` 为 None 时原样返回 task;否则与 _build_non_interactive_prompt
    等价(只换入参来源:从 skill_path 解析出 name/body)。"""

    def test_no_skill_returns_task_unchanged(self):
        assert build_live_prompt("do the thing", None) == "do the thing"

    def test_with_skill_wraps_body(self, tmp_path):
        sp = tmp_path / "skill"
        sp.mkdir()
        (sp / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: d\n---\n# Steps\n1. do X\n",
            encoding="utf-8",
        )
        out = build_live_prompt("the task", sp)
        assert "<skill name=\"myskill\">" in out
        assert "do X" in out                       # skill body 注入
        assert out.splitlines()[-1] == "the task"  # task 在末尾
        # 与 _build_non_interactive_prompt 等价(只换入参来源);skill_body 从同一
        # parse_skill_md 取,避免硬编码与真实解析结果漂移(parse_skill_md 会去尾换行)。
        from skill_eval.parser import parse_skill_md
        name, _desc, _content, _refs, body = parse_skill_md(sp)
        assert out == _build_non_interactive_prompt("the task", name, body)


class TestRunAgent:
    """run_agent:agent 正文(frontmatter 后 body)经 ``--append-system-prompt`` 注入 cc 的
    system 层(prompt 原样),测 agent 绝对质量(单臂,无 baseline)。区别于 run()(skill
    body 包 ``<skill>`` 拼 user prompt)——agent 用 cc 原生 system prompt 注入,贴近真实 dispatch。"""

    def _make_agent_md(self, tmp_path):
        p = tmp_path / "agent.md"
        p.write_text(
            "---\nname: reviewer\ndescription: ROUTE HINT KEEP OUT\n"
            "tools: [Read]\n---\n# 角色\nagent body line here\n",
            encoding="utf-8",
        )
        return p

    def _mock_spawn_eof(self, monkeypatch):
        """mock Popen:proc 立即 EOF(returncode 0)→ _spawn 返空 stdout 正常路径。"""
        from unittest.mock import MagicMock
        import skill_eval.execution.cli_runner as mod
        proc = MagicMock()
        proc.stdout.readline.return_value = b""
        proc.stderr.read.return_value = b""
        proc.wait.return_value = 0
        proc.returncode = 0
        mock_popen = MagicMock(return_value=proc)
        monkeypatch.setattr(mod.subprocess, "Popen", mock_popen)
        return mock_popen

    def test_run_agent_appends_system_prompt_with_body(self, monkeypatch, tmp_path):
        agent_md = self._make_agent_md(tmp_path)
        mock_popen = self._mock_spawn_eof(monkeypatch)
        ClaudeRunner().run_agent(prompt="the task", agent_path=agent_md, cwd=tmp_path)
        cmd = mock_popen.call_args[0][0]
        assert "--append-system-prompt" in cmd
        idx = cmd.index("--append-system-prompt")
        body_arg = cmd[idx + 1]
        assert "agent body line here" in body_arg       # frontmatter 后正文注入 system 层
        assert "ROUTE HINT KEEP OUT" not in body_arg     # description 是路由线索,不进 system prompt
        assert "<skill name=" not in body_arg            # 不走 build_live_prompt(skill 拼 user prompt 那套)

    def test_run_agent_prompt_passed_raw(self, monkeypatch, tmp_path):
        agent_md = self._make_agent_md(tmp_path)
        mock_popen = self._mock_spawn_eof(monkeypatch)
        ClaudeRunner().run_agent(prompt="do the thing", agent_path=agent_md, cwd=tmp_path)
        cmd = mock_popen.call_args[0][0]
        p_idx = cmd.index("-p")
        assert cmd[p_idx + 1] == "do the thing"          # prompt 原样传(不像 run() 经 build_live_prompt)
        assert "NON-INTERACTIVE" not in cmd[p_idx + 1]   # 未被 _build_non_interactive_prompt 包裹
        assert "<skill name=" not in cmd[p_idx + 1]
