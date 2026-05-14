"""Blind Comparator — compare two outputs without knowing which skill produced them."""

from __future__ import annotations

import json
from pathlib import Path

from skill_eval.models import ComparisonResult
from skill_eval.utils import call_claude


class BlindComparator:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self._prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        prompt_path = Path(__file__).parent.parent.parent.parent / "agents" / "comparator.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    def compare(
        self,
        output_a: str,
        output_b: str,
        eval_prompt: str,
        expectations: list[str] | None = None,
    ) -> ComparisonResult:
        prompt = self._build_prompt(output_a, output_b, eval_prompt, expectations)
        response = self._call(prompt)
        return self._parse(response)

    def _build_prompt(
        self,
        output_a: str,
        output_b: str,
        eval_prompt: str,
        expectations: list[str] | None,
    ) -> str:
        parts = [
            self._prompt_template,
            "",
            "--- COMPARISON TASK ---",
            "",
            f"Eval Prompt: {eval_prompt}",
            "",
            "--- OUTPUT A ---",
            output_a[:8000],
            "",
            "--- OUTPUT B ---",
            output_b[:8000],
        ]
        if expectations:
            parts.append("")
            parts.append("--- EXPECTATIONS ---")
            for e in expectations:
                parts.append(f"- {e}")
        parts.append("")
        parts.append("Write your comparison as JSON matching the comparison schema. All string values (reasoning, strengths, weaknesses, etc.) must be in Chinese (中文).")
        return "\n".join(parts)

    def _call(self, prompt: str) -> str:
        try:
            return call_claude(prompt, model=self.model, timeout=self.timeout, max_turns=1)
        except RuntimeError:
            return ""

    def _parse(self, response: str) -> ComparisonResult:
        if not response:
            return ComparisonResult(winner="TIE", reasoning="对比调用失败")
        try:
            data = json.loads(response)
            return ComparisonResult.model_validate(data)
        except (json.JSONDecodeError, Exception):
            import re
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if match:
                try:
                    return ComparisonResult.model_validate(json.loads(match.group()))
                except Exception:
                    pass
            return ComparisonResult(winner="TIE", reasoning="无法解析对比响应")
