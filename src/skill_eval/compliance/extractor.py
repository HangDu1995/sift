"""Instruction extractor — extracts checkable instructions from SKILL.md body."""

from __future__ import annotations

import re

from skill_eval.i18n import ANTI_FABRICATION_CN  # 反捏造词与 static_analysis/checks.py 共用(P2-13)
from skill_eval.models import (
    CheckMethod,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
)

# 中文关键词 — extractor 独用(单模块词表留原处);反捏造词已移至 skill_eval.i18n 共享(P2-13)。
_CONSTRAINT_KEYWORDS_CN: tuple[str, ...] = ("禁止", "严禁", "不得", "不应", "不可", "必须")
_TOOL_VERBS_CN: tuple[str, ...] = ("运行", "执行", "调用", "启动")


class InstructionExtractor:
    def extract(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        sections = self._split_sections(body)

        for section_name, section_text in sections:
            checks.extend(self._extract_heading_steps(section_name, section_text))
            checks.extend(self._extract_tool_requirements(section_name, section_text))

        checks.extend(self._extract_constraints(body))
        checks.extend(self._extract_anti_fabrication(body))
        checks.extend(self._extract_output_format(body))
        checks.extend(self._extract_reference_usage(body))

        return checks

    def _split_sections(self, body: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_heading = "Root"
        current_lines: list[str] = []

        for line in body.split("\n"):
            if line.startswith("#"):
                if current_lines:
                    sections.append((current_heading, "\n".join(current_lines)))
                current_heading = line.lstrip("#").strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            sections.append((current_heading, "\n".join(current_lines)))

        return sections

    def _extract_heading_steps(self, section: str, text: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        lines = text.strip().split("\n")
        imperative_re = re.compile(r"^[*\-]\s+\**[A-Z]")

        for line in lines:
            stripped = line.strip()
            m = re.match(r"^\d+\.\s+", stripped)
            if not m and not stripped.startswith(("* ", "- ")):
                continue
            clean = re.sub(r"^[*\-]\s+|\d+\.\s+", "", stripped).strip()
            clean = re.sub(r"\*\*", "", clean)
            if not clean or len(clean) < 5:
                continue

            checks.append(InstructionCheck(
                source=InstructionSource.HEADING_STEP,
                instruction_text=clean,
                section=section,
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.LLM_JUDGE,
            ))

        return checks

    def _extract_tool_requirements(self, section: str, text: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        # 英文:动词 + 必有空格 + 可选 "the" + 工具名。\s+ 防 "using" 被拆成 use+ing。
        # 中文:动词(常无空格)+ 英文工具名(中文工具名无法对应 trace 里的工具调用,不提取)。
        patterns = [
            re.compile(r"(?:run|execute|use|call|invoke)\s+(?:the\s+)?(\w+\.?\w*)", re.IGNORECASE),
            re.compile(rf"(?:{'|'.join(_TOOL_VERBS_CN)})\s*(\w+\.?\w*)"),
        ]
        for pattern in patterns:
            for match in pattern.finditer(text):
                tool_name = match.group(1)
                checks.append(InstructionCheck(
                    source=InstructionSource.TOOL_REQUIREMENT,
                    instruction_text=match.group(0).strip(),
                    section=section,
                    priority=InstructionPriority.REQUIRED,
                    check_method=CheckMethod.TOOL_CALL_CHECK,
                    params={"tool_name": tool_name},
                ))
        return checks

    def _extract_constraints(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        # 中文约束词后跟至少 2 字内容(中文信息密度高,2 字 ≈ 英文 5 字);`.` 默认不跨行。
        cn = "|".join(_CONSTRAINT_KEYWORDS_CN)
        pattern = re.compile(
            r"(NEVER|ALWAYS|MUST\s+NOT|DO\s+NOT|SHALL\s+NOT|SHOULD\s+NOT)\s+.{5,}"
            rf"|(?:{cn}).{{2,}}",
            re.IGNORECASE,
        )
        for match in pattern.finditer(body):
            checks.append(InstructionCheck(
                source=InstructionSource.CONSTRAINT,
                instruction_text=match.group(0).strip(),
                section="Constraint",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.LLM_JUDGE,
            ))
        return checks

    def _extract_anti_fabrication(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        lower = body.lower()
        has_af = (
            "anti-fabrication" in lower
            or "never fabricate" in lower
            or "do not fabricate" in lower
            or any(w in body for w in ANTI_FABRICATION_CN)
        )
        if not has_af:
            return checks
        for line in body.split("\n"):
            ls = line.strip().lower()
            if "fabricat" in ls or "invent" in ls or "hallucinat" in ls or any(w in line for w in ANTI_FABRICATION_CN):
                checks.append(InstructionCheck(
                    source=InstructionSource.ANTI_FABRICATION,
                    instruction_text=line.strip(),
                    section="Anti-Fabrication",
                    priority=InstructionPriority.REQUIRED,
                    check_method=CheckMethod.LLM_JUDGE,
                ))
        return checks

    def _extract_output_format(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        if "```" in body:
            blocks = re.findall(r"```[\w]*\n(.*?)```", body, re.DOTALL)
            for block in blocks[:3]:
                checks.append(InstructionCheck(
                    source=InstructionSource.OUTPUT_FORMAT,
                    instruction_text=f"Output should follow template format",
                    section="Format",
                    priority=InstructionPriority.RECOMMENDED,
                    check_method=CheckMethod.PATTERN_MATCH,
                    params={"sample": block[:200]},
                ))
        return checks

    def _extract_reference_usage(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        refs = re.findall(r"references/[\w\-\.]+|scripts/[\w\-\.]+", body)
        for ref in set(refs):
            checks.append(InstructionCheck(
                source=InstructionSource.REFERENCE_USAGE,
                instruction_text=f"Use reference file: {ref}",
                section="References",
                priority=InstructionPriority.RECOMMENDED,
                check_method=CheckMethod.FILE_PRESENCE,
                params={"path": ref},
            ))
        return checks
