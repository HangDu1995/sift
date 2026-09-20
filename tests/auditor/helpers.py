"""Shared auditor test data builders."""

from __future__ import annotations

import json
from pathlib import Path

from sift.models import InstructionCheck
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


def _tool_inst(text: str = "必须使用 Bash 工具", tool_name: str = "Bash") -> InstructionCheck:
    """造一条 TOOL_REQUIREMENT(params 含 tool_name)。"""
    return InstructionCheck(
        source=InstructionSource.TOOL_REQUIREMENT,
        instruction_text=text,
        section="Tools",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"tool_name": tool_name},
    )


def _ref_inst(text: str = "读取 design.md", script_path: str = "design.md") -> InstructionCheck:
    """造一条 REFERENCE_USAGE(params 含 script_path)。"""
    return InstructionCheck(
        source=InstructionSource.REFERENCE_USAGE,
        instruction_text=text,
        section="References",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK,
        params={"script_path": script_path},
    )


def _tc(name: str, input_summary: str = "") -> dict:
    """造一个 dict 风格的 tool_trace 元素(match_tool_call 支持 attr / dict)。"""
    return {"name": name, "input_summary": input_summary}


def _prohibition_inst(
    text: str = "禁止编造测试数据",
    source: InstructionSource = InstructionSource.CONSTRAINT,
) -> InstructionCheck:
    """造一条禁令类指令(CONSTRAINT / ANTI_FABRICATION)。"""
    return InstructionCheck(
        source=source,
        instruction_text=text,
        section="Constraints",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _step_inst(text: str = "Step 1: 读取需求文档") -> InstructionCheck:
    """造一条步骤类指令(HEADING_STEP)。"""
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text=text,
        section="Process",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


def _output_inst(text: str = "输出代码块", sample: str = "```python\nprint('hi')\n```") -> InstructionCheck:
    """造一条 OUTPUT_FORMAT 指令(params 含 sample,check_method=PATTERN_MATCH)。"""
    return InstructionCheck(
        source=InstructionSource.OUTPUT_FORMAT,
        instruction_text=text,
        section="Output",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.PATTERN_MATCH,
        params={"sample": sample},
    )


def _pass_fake():
    """通用 mock call_claude:返回全"通过"语义的 JSON 数组(覆盖 0..59)。

    - 条件性未命中 → applicable=false → N/A(场景不涉及)
    - 禁令 → violated=false → PASS(没违规)
    - 步骤 → verdict="pass" → PASS(走过了)

    一个结构喂三个判定方法,便于聚合测试数 finding / 算 summary。
    """

    def fake(prompt, *a, **k):
        return json.dumps(
            [
                {
                    "index": i,
                    "applicable": False,
                    "violated": False,
                    "verdict": "pass",
                    "evidence": "通过",
                }
                for i in range(60)
            ]
        )

    return fake


def _headline_fake():
    """通用 mock call_claude:全通过 + 带 headline(供 headline 生成/透传测试)。

    一个结构兼容三种判定格式 + headline(key 各取所需):headline 步骤读 headline,
    conditional 读 applicable、prohibition 读 violated、step 读 verdict。
    """

    def fake(prompt, *a, **k):
        return json.dumps(
            [
                {
                    "index": i,
                    "applicable": False,
                    "violated": False,
                    "verdict": "pass",
                    "evidence": "通过",
                    "headline": f"标题 {i}",
                }
                for i in range(60)
            ]
        )

    return fake


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """把 records 写成 JSONL(每行一个 JSON 对象),返回该路径。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _read_file_records(
    file_path: str,
    content: str = "文件内容",
    user_text: str = "请分析这个文件",
) -> list[dict]:
    """造一组 session JSONL 记录:user turn + Read(file_path) + 其 tool_result。

    让 build_digest 产出非空 digest:user_turns 非空、tool_trace 含 Read、
    input_files 含 file_path(未被改写 → 计入输入),供反推/对账用。
    """
    return [
        {"message": {"role": "user", "content": user_text}},
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": file_path},
                    }
                ],
            }
        },
        {
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": content}],
            }
        },
    ]


class _FakeExtractor:
    """替身:跳过 extract 正则,直接返回造好的指令(audit() 内 ``InstructionExtractor().extract``)。"""

    def __init__(self, insts):
        self._insts = insts

    def extract(self, body):
        return list(self._insts)


_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "example-skills"
