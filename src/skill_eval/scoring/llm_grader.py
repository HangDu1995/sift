"""LLM Grader — semantic evaluation via claude -p."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from skill_eval.models import EvalCase, GradingResult


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
    ) -> GradingResult:
        prompt = self._build_prompt(eval_case, transcript, output_dir)
        response = self._call_claude(prompt)

        try:
            data = json.loads(response)
            return GradingResult.model_validate(data)
        except (json.JSONDecodeError, Exception):
            return self._parse_response(response, eval_case)

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
            + "\n\n--- ASSERTIONS ---\n"
            + "\n".join(f"- {a.name} ({a.type}): {a.params}" for a in eval_case.assertions)
            + "\n\nWrite your grading as JSON matching the grading.json schema."
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

    def _call_claude(self, prompt: str) -> str:
        cmd = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions", "--max-turns", "1"]
        if self.model:
            cmd.extend(["--model", self.model])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            stdout_bytes, _ = proc.communicate(input=prompt.encode("utf-8"), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            return ""
        return stdout_bytes.decode("utf-8", errors="replace")

    def _parse_response(self, response: str, eval_case: EvalCase) -> GradingResult:
        json_match = None
        import re
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return GradingResult.model_validate(json.loads(match.group()))
            except Exception:
                pass

        from skill_eval.models import ExpectationResult, GradingSummary
        return GradingResult(
            expectations=[
                ExpectationResult(text=e, passed=False, evidence="LLM response could not be parsed", source="llm")
                for e in eval_case.expectations
            ],
            summary=GradingSummary(passed=0, failed=len(eval_case.expectations), total=len(eval_case.expectations), pass_rate=0.0),
        )
