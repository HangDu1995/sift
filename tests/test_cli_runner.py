"""Tests for Claude CLI runner: prompt construction + stream-json token parsing."""

from __future__ import annotations

import json

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


class TestParseStreamJsonTokenExtraction:
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
