"""Post-hoc Analyzer — analyze benchmark results and generate insights."""

from __future__ import annotations

import json
from pathlib import Path

from sift.models import AnalysisResult
from sift.concurrency import _LLMRetryExhausted, call_llm_with_retry
from sift.scoring._jsonschema import inline_schema

# 结构化输出 schema:benchmark notes 判官走 --json-schema(JSON array of string)。
# CLI 拒顶层 array,故包 items;call_claude 透明解开 → _parse_notes 照常命中,正则 fallback 兜底。
_NOTES_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
}

# analyze_comparison() 给 LLM 的结构化 schema(forced tool_use 用)。AnalysisResult 全
# 字段都是真分析产出(winner_strengths/loser_weaknesses/instruction_following/
# improvement_suggestions/transcript_insights)、全有默认,无 internal 可剔。最后经
# inline_schema 展开 $ref(同 grader:部分端点不解析 tool input_schema 里的 $ref →
# 返回空 → analyze_comparison 解析失败 → None → skip。inline 修复)。
_ANALYSIS_FULL = AnalysisResult.model_json_schema()
ANALYSIS_SCHEMA: dict = inline_schema({
    "type": "object",
    "properties": _ANALYSIS_FULL["properties"],
    "required": [],
    **({"$defs": _ANALYSIS_FULL["$defs"]} if "$defs" in _ANALYSIS_FULL else {}),
})


class PosthocAnalyzer:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self._prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        prompt_path = Path(__file__).parent.parent.parent.parent / "agents" / "analyzer.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    def analyze_benchmark(
        self,
        benchmark_json: str,
        skill_description: str = "",
        *,
        on_usage=None,
    ) -> list[str]:
        prompt = self._build_benchmark_prompt(benchmark_json, skill_description)
        response = self._call(prompt, on_usage=on_usage, schema=_NOTES_SCHEMA)
        return self._parse_notes(response)

    def analyze_comparison(
        self,
        winner: str,
        winner_skill: str,
        loser_skill: str,
        comparison_reasoning: str,
        winner_transcript: str,
        loser_transcript: str,
        *,
        focus_skill: str,
        on_usage=None,
    ) -> AnalysisResult | None:
        prompt = self._build_comparison_prompt(
            winner, winner_skill, loser_skill,
            comparison_reasoning, winner_transcript, loser_transcript,
            focus_skill=focus_skill,
        )
        response = self._call(prompt, on_usage=on_usage, schema=ANALYSIS_SCHEMA)
        return self._parse_analysis(response)

    def _build_benchmark_prompt(self, benchmark_json: str, skill_description: str) -> str:
        parts = [
            self._prompt_template,
            "",
            "--- BENCHMARK ANALYSIS TASK ---",
            "",
        ]
        if skill_description:
            parts.append(f"Skill Description: {skill_description[:2000]}")
            parts.append("")
        parts.append("--- BENCHMARK DATA ---")
        parts.append(benchmark_json[:12000])
        parts.append("")
        parts.append("Generate analysis notes as a JSON array of strings. All string values must be in Chinese (中文).")
        return "\n".join(parts)

    def _build_comparison_prompt(
        self,
        winner: str,
        winner_skill: str,
        loser_skill: str,
        comparison_reasoning: str,
        winner_transcript: str,
        loser_transcript: str,
        *,
        focus_skill: str,
    ) -> str:
        parts = [
            self._prompt_template,
            "",
            "--- COMPARISON ANALYSIS TASK ---",
            "",
            f"Winner: {winner}",
            f"Comparator Reasoning: {comparison_reasoning[:2000]}",
            "",
            "--- WINNER SKILL ---",
            winner_skill[:4000],
            "",
            "--- LOSER SKILL ---",
            loser_skill[:4000],
            "",
            "--- FOCUS SKILL (改进对象;improvement_suggestions 必须针对它)---",
            focus_skill[:4000],
            "",
            "--- WINNER TRANSCRIPT ---",
            winner_transcript[:4000],
            "",
            "--- LOSER TRANSCRIPT ---",
            loser_transcript[:4000],
            "",
            "Write your analysis as JSON matching the analysis schema. All string values (strengths, weaknesses, suggestions, etc.) must be in Chinese (中文).",
        ]
        return "\n".join(parts)

    def _call(self, prompt: str, *, on_usage=None, schema=None) -> str:
        try:
            return call_llm_with_retry(prompt, model=self.model, timeout=self.timeout, schema=schema, on_usage=on_usage, transport="http")
        except _LLMRetryExhausted:
            return ""

    def _parse_notes(self, response: str) -> list[str]:
        if not response:
            return []
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [str(item) for item in data]
        except json.JSONDecodeError:
            import re
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                    if isinstance(data, list):
                        return [str(item) for item in data]
                except Exception:
                    pass
        return [line.strip().lstrip("- ") for line in response.splitlines() if line.strip() and not line.startswith("#")]

    def _parse_analysis(self, response: str) -> AnalysisResult | None:
        if not response:
            return None
        try:
            data = json.loads(response)
            return AnalysisResult.model_validate(data)
        except (json.JSONDecodeError, Exception):
            import re
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if match:
                try:
                    return AnalysisResult.model_validate(json.loads(match.group()))
                except Exception:
                    pass
            return None
