"""Instruction extractor — extracts checkable instructions from SKILL.md body."""

from __future__ import annotations

import re

from skill_eval.models import (
    CheckMethod,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
)


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
        pattern = re.compile(r"(?:run|execute|use|call|invoke)\s+(?:the\s+)?(\w+\.?\w*)", re.IGNORECASE)
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
        pattern = re.compile(r"(NEVER|ALWAYS|MUST\s+NOT|DO\s+NOT|SHALL\s+NOT|SHOULD\s+NOT)\s+.{5,}", re.IGNORECASE)
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
        if "anti-fabrication" in lower or "never fabricate" in lower or "do not fabricate" in lower:
            for line in body.split("\n"):
                ls = line.strip().lower()
                if "fabricat" in ls or "invent" in ls or "hallucinat" in ls:
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
