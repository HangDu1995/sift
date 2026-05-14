"""Compliance checker — verifies process followed SKILL.md instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path

from skill_eval.models import (
    CheckMethod,
    ComplianceReport,
    ComplianceResultItem,
    InstructionCheck,
    InstructionPriority,
)
from skill_eval.utils import call_claude

_BATCH_TIMEOUT = 90
_SINGLE_TIMEOUT = 30
_MAX_LLM_IN_BATCH = 30


class ComplianceChecker:
    def check(
        self,
        instructions: list[InstructionCheck],
        transcript: str,
        output_dir: Path,
        model: str | None = None,
        timeout: int = 120,
        tool_trace: list | None = None,
        on_substep=None,
        log_path: Path | None = None,
    ) -> ComplianceReport:
        total = len(instructions)
        if log_path:
            log_path.write_text(f"Compliance check: {total} instructions\n", encoding="utf-8")

        llm_instructions = []
        llm_indices = []
        results: dict[int, ComplianceResultItem] = {}

        for idx, instruction in enumerate(instructions):
            if instruction.check_method == CheckMethod.LLM_JUDGE:
                llm_instructions.append(instruction)
                llm_indices.append(idx)
            else:
                item = self._check_instruction(instruction, transcript, output_dir, model, timeout, tool_trace)
                results[idx] = item
                self._log_step(log_path, idx + 1, total, instruction.instruction_text[:80], item)
                if on_substep:
                    on_substep(idx + 1, total, instruction.instruction_text[:60], item.followed, item.confidence)

        if llm_instructions:
            batch_instructions = llm_instructions[:_MAX_LLM_IN_BATCH]
            batch_indices = llm_indices[:_MAX_LLM_IN_BATCH]
            remaining_indices = llm_indices[_MAX_LLM_IN_BATCH:]

            batch_results = self._check_llm_batch(batch_instructions, transcript, model, _BATCH_TIMEOUT)

            for i, item in enumerate(batch_results):
                idx = batch_indices[i]
                results[idx] = item
                self._log_step(log_path, idx + 1, total, batch_instructions[i].instruction_text[:80], item)
                if on_substep:
                    on_substep(idx + 1, total, batch_instructions[i].instruction_text[:60], item.followed, item.confidence)

            for idx in remaining_indices:
                results[idx] = ComplianceResultItem(
                    instruction=instructions[idx],
                    followed=False,
                    evidence=f"Skipped: LLM batch budget ({_MAX_LLM_IN_BATCH}) exhausted",
                    confidence=0.0,
                    source="llm",
                )
                self._log_step(log_path, idx + 1, total, instructions[idx].instruction_text[:80], results[idx])

        items = [results[i] for i in range(total)]
        followed = sum(1 for i in items if i.followed)
        required_items = [i for i in items if i.instruction.priority == InstructionPriority.REQUIRED]
        required_followed = sum(1 for i in required_items if i.followed)

        return ComplianceReport(
            skill_name="",
            total_instructions=total,
            followed_count=followed,
            compliance_rate=followed / total if total > 0 else 0.0,
            checks=items,
            warnings=[],
        )

    @staticmethod
    def _log_step(log_path: Path | None, step: int, total: int, text: str, item: ComplianceResultItem):
        if not log_path:
            return
        tag = "PASS" if item.followed else "FAIL"
        line = f"[{tag}] {step}/{total} [{item.source}] conf={item.confidence:.1f} {text}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _check_instruction(
        self,
        instruction: InstructionCheck,
        transcript: str,
        output_dir: Path,
        model: str | None,
        timeout: int,
        tool_trace: list | None = None,
    ) -> ComplianceResultItem:
        if instruction.check_method == CheckMethod.TOOL_CALL_CHECK:
            return self._check_tool_call(instruction, transcript, tool_trace)
        elif instruction.check_method in (CheckMethod.PATTERN_MATCH, CheckMethod.FILE_PRESENCE):
            return self._check_programmatic(instruction, transcript, output_dir)
        else:
            return self._check_llm_single(instruction, transcript, model, _SINGLE_TIMEOUT)

    def _check_tool_call(self, instruction: InstructionCheck, transcript: str, tool_trace: list | None = None) -> ComplianceResultItem:
        tool_name = instruction.params.get("tool_name", "")
        script_path = instruction.params.get("script_path", "")
        lower = transcript.lower()

        if tool_trace:
            for tc in tool_trace:
                tc_name = tc.name if hasattr(tc, "name") else tc.get("name", "")
                tc_input = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
                if tool_name and tool_name.lower() in tc_name.lower():
                    return ComplianceResultItem(
                        instruction=instruction,
                        followed=True,
                        evidence=f"Tool '{tc_name}' found in tool trace with input: {tc_input[:100]}",
                        confidence=1.0,
                        source="programmatic",
                    )
                if script_path and script_path.lower() in tc_input.lower():
                    return ComplianceResultItem(
                        instruction=instruction,
                        followed=True,
                        evidence=f"Script path '{script_path}' found in tool call '{tc_name}': {tc_input[:100]}",
                        confidence=1.0,
                        source="programmatic",
                    )

        tool_used = tool_name.lower() in lower if tool_name else False
        script_found = script_path.lower() in lower if script_path else False

        return ComplianceResultItem(
            instruction=instruction,
            followed=tool_used or script_found,
            evidence=f"Tool '{tool_name}' {'found' if tool_used else 'not found'} in transcript"
                      + (f", script '{script_path}' {'found' if script_found else 'not found'}" if script_path else ""),
            confidence=1.0 if tool_trace else 0.7,
            source="programmatic",
        )

    def _check_programmatic(
        self, instruction: InstructionCheck, transcript: str, output_dir: Path
    ) -> ComplianceResultItem:
        if instruction.check_method == CheckMethod.FILE_PRESENCE:
            path = instruction.params.get("path", "")
            target = output_dir / path
            exists = target.exists()
            return ComplianceResultItem(
                instruction=instruction,
                followed=exists,
                evidence=f"File {path} {'exists' if exists else 'not found'}",
                confidence=1.0,
                source="programmatic",
            )

        sample = instruction.params.get("sample", "")
        if sample:
            match = any(line.strip() in transcript for line in sample.split("\n") if line.strip())
            return ComplianceResultItem(
                instruction=instruction,
                followed=match,
                evidence="Pattern match in transcript" if match else "Pattern not found",
                confidence=0.7 if match else 0.3,
                source="programmatic",
            )

        return ComplianceResultItem(
            instruction=instruction,
            followed=False,
            evidence="No programmatic check available",
            confidence=0.0,
            source="programmatic",
        )

    def _check_llm_batch(
        self,
        instructions: list[InstructionCheck],
        transcript: str,
        model: str | None,
        timeout: int,
    ) -> list[ComplianceResultItem]:
        instruction_list = ""
        for i, inst in enumerate(instructions):
            instruction_list += (
                f"[{i}] Section: {inst.section} | Priority: {inst.priority}\n"
                f"    {inst.instruction_text}\n\n"
            )

        prompt = (
            f"You are a compliance checker. Determine if each instruction was followed during execution.\n\n"
            f"## Instructions to check ({len(instructions)} total)\n\n"
            f"{instruction_list}\n"
            f"## Transcript excerpt\n\n{transcript[:8000]}\n\n"
            f"## Output Format\n\n"
            f"Respond with ONLY a JSON array. Each element:\n"
            f'{{"index": 0, "followed": true/false, "evidence": "brief explanation", "confidence": 0.0-1.0}}\n\n'
            f"Judge each instruction independently. Be strict: if no clear evidence, mark followed=false.\n"
            f"All evidence strings must be in Chinese (中文)."
        )

        try:
            result_text = call_claude(prompt, model=model, timeout=timeout, max_turns=1)
            return self._parse_batch_result(result_text, instructions)
        except RuntimeError:
            return [
                ComplianceResultItem(
                    instruction=inst,
                    followed=False,
                    evidence="LLM 批量检查不可用",
                    confidence=0.0,
                    source="llm",
                )
                for inst in instructions
            ]

    @staticmethod
    def _parse_batch_result(
        text: str, instructions: list[InstructionCheck]
    ) -> list[ComplianceResultItem]:
        defaults = [
            ComplianceResultItem(
                instruction=inst,
                followed=False,
                evidence="LLM 未评估此指令",
                confidence=0.0,
                source="llm",
            )
            for inst in instructions
        ]

        array_match = re.search(r"\[[\s\S]*\]", text)
        if not array_match:
            return defaults

        try:
            items = json.loads(array_match.group())
        except json.JSONDecodeError:
            return defaults

        if not isinstance(items, list):
            return defaults

        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(defaults):
                continue
            defaults[idx] = ComplianceResultItem(
                instruction=instructions[idx],
                followed=bool(item.get("followed", False)),
                evidence=str(item.get("evidence", "")),
                confidence=float(item.get("confidence", 0.5)),
                source="llm",
            )

        return defaults

    def _check_llm_single(
        self,
        instruction: InstructionCheck,
        transcript: str,
        model: str | None,
        timeout: int,
    ) -> ComplianceResultItem:
        prompt = (
            f"You are a compliance checker. Determine if the following instruction was followed "
            f"during execution.\n\n"
            f"Instruction: {instruction.instruction_text}\n"
            f"Section: {instruction.section}\n"
            f"Priority: {instruction.priority}\n\n"
            f"Transcript excerpt:\n{transcript[:4000]}\n\n"
            f"Respond with JSON: {{\"followed\": true/false, \"evidence\": \"explanation\", \"confidence\": 0.0-1.0}}\n"
            f"All evidence strings must be in Chinese (中文)."
        )

        try:
            result_text = call_claude(prompt, model=model, timeout=timeout, max_turns=1)
            match = re.search(r"\{[^}]+\}", result_text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return ComplianceResultItem(
                    instruction=instruction,
                    followed=bool(data.get("followed", False)),
                    evidence=str(data.get("evidence", "")),
                    confidence=float(data.get("confidence", 0.5)),
                    source="llm",
                )
        except Exception:
            pass

        return ComplianceResultItem(
            instruction=instruction,
            followed=False,
            evidence="LLM 检查不可用",
            confidence=0.0,
            source="llm",
        )
