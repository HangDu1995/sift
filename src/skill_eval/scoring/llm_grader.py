"""LLM Grader — semantic evaluation via claude -p."""

from __future__ import annotations

import json
import re
from pathlib import Path

from skill_eval.models import (
    AssertionVerdict,
    EvalCase,
    ExpectationResult,
    GradingResult,
    GradingSummary,
)
from skill_eval.utils import call_claude

# 结构化输出 schema:tier3 判官走 --json-schema,约束 JSON 不可畸形(CLI 拒顶层 array,
# 故包 items;call_claude 透明解开 → _extract_json_array 照常命中,正则 fallback 兜底)。
_TIER3_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["PASS", "FAIL", "UNEVAL"]},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["index", "verdict", "evidence"],
            },
        }
    },
    "required": ["items"],
}


class LLMGrader:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self._prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        prompt_path = Path(__file__).parent.parent.parent.parent / "agents" / "grader.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    def grade(
        self,
        eval_case: EvalCase,
        transcript: str,
        output_dir: Path,
        *,
        on_usage=None,
    ) -> GradingResult:
        prompt = self._build_prompt(eval_case, transcript, output_dir)
        response = self._call_claude(prompt, on_usage=on_usage)

        if not response or not response.strip():
            return self._build_uneval_result(eval_case, "LLM grading returned empty response")

        try:
            data = json.loads(response)
            result = GradingResult.model_validate(data)
            self._normalize_verdicts(result, eval_case)
            return result
        except (json.JSONDecodeError, Exception):
            return self._parse_response(response, eval_case)

    def grade_tier3(
        self,
        tier3_assertions: list,
        transcript: str,
        output_dir: Path,
        *,
        on_usage=None,
    ) -> list[ExpectationResult]:
        """Grade Tier 3 assertions specifically with evidence/citation requirements."""
        if not tier3_assertions:
            return []

        outputs = self._collect_outputs(output_dir)
        prompt = self._build_tier3_prompt(tier3_assertions, transcript, outputs)
        response = self._call_claude(prompt, on_usage=on_usage, schema=_TIER3_SCHEMA)

        if not response or not response.strip():
            return [
                ExpectationResult(
                    text=a.name,
                    passed=False,
                    evidence="Grading returned empty response",
                    source="llm",
                    verdict=AssertionVerdict.UNEVAL,
                )
                for a in tier3_assertions
            ]

        return self._parse_tier3_response(response, tier3_assertions)

    def _build_tier3_prompt(
        self, assertions: list, transcript: str, outputs: str
    ) -> str:
        lines = []
        for i, a in enumerate(assertions):
            lines.append(f"### Assertion {i}: {a.name}")
            lines.append(f"Type: {a.type.value}")
            lines.append(f"Params: {json.dumps(a.params, ensure_ascii=False)}")
            lines.append("")
        return (
            f"{self._prompt_template}\n\n"
            f"--- TIER 3 GRADING TASK ---\n\n"
            f"You are grading ONLY Tier 3 (semantic quality) assertions. "
            f"For each assertion, determine whether the agent's output satisfies it.\n\n"
            f"## Assertions to Grade\n\n"
            + "\n".join(lines)
            + f"\n## Transcript\n{transcript[:12000]}\n\n"
            f"## Outputs\n{outputs[:8000]}\n\n"
            f"## Response Format\n\n"
            f"Respond with ONLY a JSON array:\n"
            f'[{{"index": 0, "verdict": "PASS", "evidence": "specific citation from transcript or output", "confidence": 0.9}}]\n\n'
            f"## Verdict Values\n"
            f"- PASS: Clear evidence the assertion is satisfied. REQUIRED: cite the exact text.\n"
            f"- FAIL: No evidence or contradictory evidence.\n"
            f"- UNEVAL: Cannot determine from available information (e.g. truncated transcript). "
            f"Use this sparingly — only when genuinely unable to judge.\n\n"
            f"All evidence strings must be in Chinese (中文)."
        )

    def _parse_tier3_response(
        self, response: str, assertions: list
    ) -> list[ExpectationResult]:
        results: list[ExpectationResult] = []
        items = self._extract_json_array(response)
        if not items:
            for a in assertions:
                results.append(
                    ExpectationResult(
                        text=a.name,
                        passed=False,
                        evidence="Failed to parse LLM grading response",
                        source="llm",
                        verdict=AssertionVerdict.UNEVAL,
                    )
                )
            return results

        results_by_index: dict[int, dict] = {}
        for item in items:
            if isinstance(item, dict):
                idx = item.get("index")
                if isinstance(idx, int):
                    results_by_index[idx] = item

        for i, a in enumerate(assertions):
            entry = results_by_index.get(i, {})
            verdict_str = entry.get("verdict", "FAIL").upper()
            try:
                verdict = AssertionVerdict(verdict_str)
            except ValueError:
                verdict = AssertionVerdict.UNEVAL

            evidence = str(entry.get("evidence", ""))
            if verdict == AssertionVerdict.UNEVAL:
                evidence = evidence or "Grading incomplete: could not determine"
            elif verdict == AssertionVerdict.PASS and not evidence:
                verdict = AssertionVerdict.UNEVAL
                evidence = "PASS verdict but no evidence provided"

            results.append(
                ExpectationResult(
                    text=a.name,
                    passed=(verdict == AssertionVerdict.PASS),
                    evidence=evidence,
                    source="llm",
                    verdict=verdict,
                )
            )

        return results

    def _normalize_verdicts(self, result: GradingResult, eval_case: EvalCase) -> None:
        for exp in result.expectations:
            if exp.evidence and exp.evidence.startswith("Not evaluated"):
                exp.verdict = AssertionVerdict.UNEVAL
                exp.passed = False

    def _build_uneval_result(self, eval_case: EvalCase, reason: str) -> GradingResult:
        from skill_eval.models import ExpectationResult, GradingSummary
        return GradingResult(
            expectations=[
                ExpectationResult(
                    text=e,
                    passed=False,
                    evidence=reason,
                    source="llm",
                    verdict=AssertionVerdict.UNEVAL,
                )
                for e in eval_case.expectations
            ],
            summary=GradingSummary(
                passed=0,
                failed=len(eval_case.expectations),
                total=len(eval_case.expectations),
                pass_rate=0.0,
            ),
        )

    def _build_prompt(self, eval_case: EvalCase, transcript: str, output_dir: Path) -> str:
        outputs = self._collect_outputs(output_dir)
        return (
            f"{self._prompt_template}\n\n"
            f"--- EVALUATION TASK ---\n\n"
            f"Eval ID: {eval_case.id}\n"
            f"Eval Name: {eval_case.get_name()}\n"
            f"Prompt: {eval_case.prompt}\n"
            f"Expected Output: {eval_case.expected_output}\n\n"
            f"--- TRANSCRIPT ---\n{transcript[:8000]}\n\n"
            f"--- OUTPUTS ---\n{outputs[:8000]}\n\n"
            f"--- EXPECTATIONS ---\n"
            + "\n".join(f"- {e}" for e in eval_case.expectations)
            + "\n\n--- TIER 3 ASSERTIONS ---\n"
            + "\n".join(
                f"- {a.name} ({a.type.value}): {a.params}"
                for a in eval_case.assertions
                if a.tier == "TIER3"
            )
            + "\n\nWrite your grading as JSON matching the grading.json schema. "
            "All string values (evidence, suggestions, reasons, etc.) must be in Chinese (中文).\n"
            "IMPORTANT: If you cannot determine the verdict for any expectation, "
            'use "evidence": "Not evaluated: <reason>". Do NOT silently mark FAIL.'
        )

    def _collect_outputs(self, output_dir: Path) -> str:
        parts: list[str] = []
        for f in sorted(output_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 4000:
                        content = content[:4000] + "\n... (truncated)"
                    parts.append(f"=== {f.name} ===\n{content}")
                except Exception:
                    parts.append(f"=== {f.name} === (binary)")
        return "\n\n".join(parts) if parts else "(no output files)"

    def _call_claude(self, prompt: str, *, on_usage=None, schema=None) -> str:
        try:
            return call_claude(prompt, model=self.model, timeout=self.timeout, max_turns=1, on_usage=on_usage, schema=schema)
        except RuntimeError:
            return ""

    @staticmethod
    def _extract_json_array(response: str) -> list:
        match = re.search(r"\[[\s\S]*\]", response)
        if not match:
            return []
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return []

    def _parse_response(self, response: str, eval_case: EvalCase) -> GradingResult:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                result = GradingResult.model_validate(json.loads(match.group()))
                self._normalize_verdicts(result, eval_case)
                return result
            except Exception:
                pass

        from skill_eval.models import ExpectationResult, GradingSummary
        return GradingResult(
            expectations=[
                ExpectationResult(
                    text=e,
                    passed=False,
                    evidence="Unable to parse LLM grading response",
                    source="llm",
                    verdict=AssertionVerdict.UNEVAL,
                )
                for e in eval_case.expectations
            ],
            summary=GradingSummary(
                passed=0,
                failed=len(eval_case.expectations),
                total=len(eval_case.expectations),
                pass_rate=0.0,
            ),
        )
