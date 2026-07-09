"""rebuild: transcript_path -> (tool_trace, transcript_text, digest) 便利入口。

薄封装 ``skill_eval.evals.transcript.build_digest``:
- 错误统一:文件不存在 / transcript 无可用内容 → 抛 ``SkillEvalError``(友好消息);
  坏 JSONL 行由 ``build_digest`` 内部跳过,此处不重复处理。
- 三元组解包:直接给 auditor 顺手的 ``(tool_trace, text, digest)``。

核心解析逻辑全部在 ``build_digest``,本模块不加多余逻辑(YAGNI)。
"""
from __future__ import annotations

from pathlib import Path

from skill_eval.errors import SkillEvalError
from skill_eval.evals.transcript import SessionDigest, build_digest
from skill_eval.models import ToolCallRecord


def rebuild(
    transcript_path: Path,
) -> tuple[list[ToolCallRecord], str, SessionDigest]:
    """把 transcript JSONL 重建为 (tool_trace, transcript_text, digest)。

    Args:
        transcript_path: Claude Code session JSONL 路径。

    Returns:
        ``(digest.tool_trace, digest.transcript_text, digest)`` —— 三元组中
        前两项即同一 digest 的字段(同一引用),解包仅为调用方便。

    Raises:
        SkillEvalError: 文件不存在;或文件存在但 digest 全空
            (无 user_turn 且 manifest 空且 tool_trace 空 —— 即未产出可用内容)。
    """
    if not transcript_path.exists():
        raise SkillEvalError(f"transcript 文件不存在: {transcript_path}")

    digest = build_digest(transcript_path)

    # digest 全空:无 user turn / 无文件 / 无工具调用 → transcript 未产出可对账内容。
    if not digest.user_turns and not digest.manifest and not digest.tool_trace:
        raise SkillEvalError(
            "transcript 未产出可用内容(无 user turn / 文件 / 工具调用)"
        )

    return digest.tool_trace, digest.transcript_text, digest
