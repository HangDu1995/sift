"""Post-hoc Analyzer — analyze benchmark results and generate insights."""

from __future__ import annotations

from sift.models import AnalysisResult
from sift.scoring._jsonschema import inline_schema
from sift.scoring.client import ScoringClient
from sift.scoring.prompts import (
    ComparisonAnalysisInput,
    build_analysis_prompt,
    build_benchmark_prompt,
    load_template,
)
from sift.scoring.results import parse_analysis_notes, parse_analysis_result

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
ANALYSIS_SCHEMA: dict = inline_schema(
    {
        "type": "object",
        "properties": _ANALYSIS_FULL["properties"],
        "required": [],
        **({"$defs": _ANALYSIS_FULL["$defs"]} if "$defs" in _ANALYSIS_FULL else {}),
    }
)


class PosthocAnalyzer:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self._prompt_template = load_template("analyzer")
        self.client = ScoringClient(model, timeout)

    def analyze_benchmark(
        self,
        benchmark_json: str,
        skill_description: str = "",
        *,
        on_usage=None,
    ) -> list[str]:
        prompt = build_benchmark_prompt(self._prompt_template, benchmark_json, skill_description)
        response = self.client.complete(prompt, on_usage=on_usage, schema=_NOTES_SCHEMA)
        return parse_analysis_notes(response)

    def analyze_comparison(self, comparison: ComparisonAnalysisInput, *, on_usage=None) -> AnalysisResult | None:
        prompt = build_analysis_prompt(self._prompt_template, comparison)
        response = self.client.complete(prompt, on_usage=on_usage, schema=ANALYSIS_SCHEMA)
        return parse_analysis_result(response)
