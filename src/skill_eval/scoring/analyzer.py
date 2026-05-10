"""Post-hoc Analyzer — analyze benchmark results and generate insights."""

from __future__ import annotations

import json
from pathlib import Path

from skill_eval.models import AnalysisResult
from skill_eval.utils import call_claude


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
    ) -> list[str]:
        prompt = self._build_benchmark_prompt(benchmark_json, skill_description)
        response = self._call(prompt)
        return self._parse_notes(response)

    def analyze_comparison(
        self,
        winner: str,
        winner_skill: str,
        loser_skill: str,
        comparison_reasoning: str,
        winner_transcript: str,
        loser_transcript: str,
    ) -> AnalysisResult | None:
        prompt = self._build_comparison_prompt(
            winner, winner_skill, loser_skill,
            comparison_reasoning, winner_transcript, loser_transcript,
        )
        response = self._call(prompt)
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
        parts.append("Generate analysis notes as a JSON array of strings.")
        return "\n".join(parts)

    def _build_comparison_prompt(
        self,
        winner: str,
        winner_skill: str,
        loser_skill: str,
        comparison_reasoning: str,
        winner_transcript: str,
        loser_transcript: str,
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
            "--- WINNER TRANSCRIPT ---",
            winner_transcript[:4000],
            "",
            "--- LOSER TRANSCRIPT ---",
            loser_transcript[:4000],
            "",
            "Write your analysis as JSON matching the analysis schema.",
        ]
        return "\n".join(parts)

    def _call(self, prompt: str) -> str:
        try:
            return call_claude(prompt, model=self.model, timeout=self.timeout, max_turns=1)
        except RuntimeError:
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
