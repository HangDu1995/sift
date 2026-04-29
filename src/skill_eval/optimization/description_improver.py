"""Description improver — uses LLM to improve skill trigger descriptions."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def _call_claude(prompt: str, model: str | None = None, timeout: int = 300) -> str:
    cmd = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
    if model:
        cmd.extend(["--model", model])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, timeout=timeout)
    return result.stdout


class DescriptionImprover:
    def __init__(self, skill_name: str, skill_content: str, model: str | None = None, timeout: int = 300):
        self.skill_name = skill_name
        self.skill_content = skill_content
        self.model = model
        self.timeout = timeout

    def improve(
        self,
        current_description: str,
        eval_results: dict,
        history: list[dict] | None = None,
    ) -> str:
        history = history or []
        prompt = self._build_prompt(current_description, eval_results, history)
        response = _call_claude(prompt, self.model, self.timeout)
        new_desc = self._extract_description(response)

        if len(new_desc) > 1024:
            shorten_prompt = (
                f"Shorten this skill description to 1024 characters or fewer while keeping the key trigger information.\n\n"
                f"Description:\n{new_desc}\n\n"
                f"Write ONLY the shortened description, no explanation."
            )
            shortened = _call_claude(shorten_prompt, self.model, self.timeout)
            candidate = self._extract_description(shortened) or shortened.strip()
            if len(candidate) <= 1024:
                new_desc = candidate

        return new_desc

    def _build_prompt(self, current: str, results: dict, history: list[dict]) -> str:
        failed = [r for r in results.get("results", []) if not r["pass"]]
        passed = [r for r in results.get("results", []) if r["pass"]]

        history_text = ""
        for h in history[-3:]:
            history_text += f"\n  - Attempt: \"{h.get('description', '')[:100]}\" → {h.get('score', 'unknown')} score"

        return (
            f"You are optimizing a Claude Code skill trigger description.\n"
            f"The description determines when Claude decides to use this skill.\n\n"
            f"Skill name: {self.skill_name}\n"
            f"Current description: {current}\n\n"
            f"EVALUATION RESULTS:\n"
            f"- Passed: {len(passed)}/{len(passed) + len(failed)}\n"
            f"- Failed cases:\n"
            + "\n".join(f"  - Query: \"{r['query'][:80]}\" → trigger_rate={r.get('trigger_rate', 0):.0%}, expected_trigger={r['should_trigger']}" for r in failed)
            + f"\n\nPREVIOUS ATTEMPTS:{history_text or ' None'}\n\n"
            f"SKILL CONTENT (for context):\n{self.skill_content[:2000]}\n\n"
            f"Generate an improved description (max 1024 chars) that better handles the failed cases.\n"
            f"Write the new description inside <new_description> tags.\n"
            f"Do NOT include any other text outside the tags."
        )

    def _extract_description(self, response: str) -> str:
        match = re.search(r"<new_description>(.*?)</new_description>", response, re.DOTALL)
        if match:
            return match.group(1).strip()
        return response.strip()[:1024]
