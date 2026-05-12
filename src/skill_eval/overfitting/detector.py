"""Overfitting detector — uses LLM to review SKILL.md against evals."""

from __future__ import annotations

import json
from pathlib import Path

from skill_eval.models import OverfittingReport, SuspiciousInstruction
from skill_eval.utils import call_claude


class OverfittingDetector:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self._prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        return (
            "You are an overfitting detector. Your job is to review a SKILL.md file "
            "against its evaluation cases and identify signs of overfitting.\n\n"
            "Overfitting means the skill has been optimized too specifically for the evaluation cases "
            "rather than being generally useful. Signs include:\n\n"
            "1. **Instruction matching**: An instruction in SKILL.md directly matches an expectation "
            "from an eval case (e.g., instruction says 'NEVER do X' and eval checks X)\n"
            "2. **Description leakage**: The skill description contains keywords that closely match "
            "eval prompts, suggesting the description was tuned to trigger on test queries\n"
            "3. **Fixture memorization**: The skill references specific field names, file names, "
            "or data patterns that appear only in eval fixtures\n\n"
            "Respond with ONLY a JSON array of suspicious items, or an empty array if none found.\n\n"
            "Each item should have: line (int), snippet (str, the suspicious text), "
            "matched_eval_id (int or null), matched_expectation (str), reason (str), "
            "severity (one of: high, medium, low)."
        )

    def detect(
        self,
        skill_path: Path | None = None,
        skill_content: str = "",
        train_evals: str = "",
    ) -> list[SuspiciousInstruction]:
        if not skill_content and skill_path:
            skill_md = skill_path / "SKILL.md"
            if skill_md.exists():
                skill_content = skill_md.read_text(encoding="utf-8", errors="replace")

        if not skill_content:
            return []

        lines = skill_content.splitlines()
        prompt = (
            f"{self._prompt_template}\n\n"
            f"--- SKILL.md (with line numbers) ---\n\n"
        )
        for i, line in enumerate(lines, 1):
            prompt += f"{i:4d}: {line}\n"

        if train_evals:
            prompt += f"\n--- TRAIN EVALS ---\n{train_evals[:8000]}\n\n"
        else:
            prompt += "\n--- TRAIN EVALS ---\n(no evals provided)\n\n"

        prompt += "Identify overfitting indicators. Respond with ONLY a JSON array."

        try:
            response = call_claude(prompt, model=self.model, timeout=self.timeout, max_turns=1)
        except RuntimeError:
            return []

        return self._parse(response)

    def _parse(self, response: str) -> list[SuspiciousInstruction]:
        if not response:
            return []
        try:
            data = json.loads(response)
            if isinstance(data, list):
                return [SuspiciousInstruction.model_validate(item) for item in data]
        except (json.JSONDecodeError, Exception):
            import re
            match = re.search(r"\[[\s\S]*\]", response)
            if match:
                try:
                    data = json.loads(match.group())
                    if isinstance(data, list):
                        return [SuspiciousInstruction.model_validate(item) for item in data]
                except Exception:
                    pass
        return []

    def build_report(
        self,
        skill_name: str = "",
        suspicious: list[SuspiciousInstruction] | None = None,
        gap_data: dict | None = None,
    ) -> OverfittingReport:
        from skill_eval.models import GeneralizationGap

        suspicious = suspicious or []
        gap = None
        if gap_data:
            gap = GeneralizationGap(**gap_data)

        high_count = sum(1 for s in suspicious if s.severity == "high")
        med_count = sum(1 for s in suspicious if s.severity == "medium")
        low_count = sum(1 for s in suspicious if s.severity == "low")

        if suspicious:
            score = min(1.0, (high_count * 0.4 + med_count * 0.2 + low_count * 0.1))
            if gap and gap.verdict == "overfit":
                score = max(score, 0.7)
        elif gap:
            score = {"good": 0.1, "warn": 0.4, "overfit": 0.7}.get(gap.verdict, 0.1)
        else:
            score = 0.0

        if suspicious and gap and gap.verdict in ("warn", "overfit"):
            recommendation = (
                f"Strong overfitting detected (gap={gap.gap:.2f}, {len(suspicious)} suspicious instructions). "
                "Stop retro-fitting SKILL.md. Instead, add NEW eval cases to cover untested scenarios."
            )
        elif gap and gap.verdict == "overfit":
            recommendation = (
                f"Generalization gap is large ({gap.gap:.2f}). "
                "The skill performs much better on training evals than holdout. "
                "Stop optimizing against current evals."
            )
        elif suspicious:
            recommendation = (
                f"Found {len(suspicious)} suspicious instructions that may be overfitted to evals. "
                "Review flagged items and consider replacing specific rules with general principles."
            )
        elif gap:
            recommendation = "No overfitting detected. Generalization gap is within acceptable range."
        else:
            recommendation = "Insufficient data for overfitting assessment."

        return OverfittingReport(
            skill_name=skill_name,
            generalization_gap=gap,
            suspicious_instructions=suspicious,
            overfitting_score=score,
            recommendation=recommendation,
        )
