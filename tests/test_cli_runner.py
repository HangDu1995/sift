"""Tests for Claude CLI runner prompt construction (P1-4)."""

from __future__ import annotations

from skill_eval.execution.cli_runner import _build_non_interactive_prompt


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
