"""Diverse eval generator — generates general-domain eval cases distinct from source project."""

from __future__ import annotations

import json
import re
from pathlib import Path

from skill_eval.models import EvalCase, EvalSet, SourceProfile
from skill_eval.parser import parse_skill_md
from skill_eval.utils import call_claude


class DiverseEvalGenerator:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def generate(
        self,
        skill_path: Path,
        source_profile: SourceProfile,
        domains: list[str] | None = None,
    ) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        prompt = self._build_prompt(name, description, content, source_profile, domains)
        try:
            response = call_claude(prompt, model=self.model, timeout=self.timeout)
        except RuntimeError:
            return self._fallback_eval_set(name, description)

        eval_set = self._parse_response(response, name)
        if eval_set is not None:
            return eval_set

        return self._fallback_eval_set(name, description)

    def _build_prompt(
        self,
        name: str,
        description: str,
        content: str,
        source_profile: SourceProfile,
        domains: list[str] | None,
    ) -> str:
        domain_instruction = ""
        if domains:
            domain_list = ", ".join(domains)
            domain_instruction = (
                f"Cover these target domains: {domain_list}.\n"
                f"Do NOT generate cases from the source project's domain.\n\n"
            )
        else:
            domain_instruction = (
                "Identify 3-5 domains/scenarios that are substantially different "
                "from the source project's domain, and generate cases for each.\n\n"
            )

        return (
            "You are an expert at creating evaluation test cases for AI coding assistant skills.\n\n"
            "Given a Skill definition and a source project profile (which was used to train/optimize the skill), "
            "generate evaluation test cases that test the skill on DIFFERENT domains than the source project.\n\n"
            "## Source Project Profile\n\n"
            "The skill was optimized using a project with these characteristics:\n"
            f"- {source_profile.project_summary}\n"
            f"- File types: {json.dumps(source_profile.file_type_distribution)}\n"
            f"- Directory structure:\n{source_profile.directory_structure_summary}\n"
            f"- Domain keywords: {', '.join(source_profile.domain_keywords)}\n\n"
            f"## Sample Files from Source Project (representative snippets)\n\n"
        ) + self._format_samples(source_profile.sample_files) + (

            "\n\n## Instructions\n\n"
            f"{domain_instruction}"
            "You must generate evaluation test cases that are STRICTLY DIFFERENT from the source project. "
            "The fixture data, domain context, and patterns must not overlap with the source project.\n\n"
            "Generate both L1 (rule comprehension) and L2 (file-based execution) cases.\n\n"
            "## Assertion Types\n"
            "You may use these assertion types for L2 cases:\n"
            "- FILE_EXISTS: checks a file was created (params: filename)\n"
            "- CONTAINS_PATTERN: checks a file contains a regex pattern (params: pattern, path)\n"
            "- NOT_CONTAINS: checks a file does NOT contain a pattern (params: pattern, path)\n"
            "- JSON_HAS_FIELD: checks a JSON file has a field (params: filename, field)\n"
            "- LINE_COUNT: checks a file has expected line count (params: filename, min, max)\n"
            "- CODE_COMPILES: checks code compiles (params: path)\n\n"
            "For each L2 case, include realistic fixture files with source domain content.\n"
            "For each L1 case, test rule comprehension — ask about how the skill would handle a scenario.\n\n"
            "## Output Schema\n"
            "Output ONLY a JSON object matching this exact schema:\n"
            "```json\n"
            "{\n"
            f'  "skill_name": "{name}",\n'
            '  "evals": [\n'
            "    {\n"
            '      "id": 1,\n'
            '      "name": "L1-<domain>-<description>",\n'
            '      "level": "L1",\n'
            '      "prompt": "A specific question/scenario",\n'
            '      "expected_output": "What the correct response looks like",\n'
            '      "expectations": ["verifiable expectation statements"],\n'
            '      "assertions": [],\n'
            '      "fixtures": []\n'
            "    },\n"
            "    {\n"
            '      "id": 2,\n'
            '      "name": "L2-<domain>-<description>",\n'
            '      "level": "L2",\n'
            '      "prompt": "A file-based task description",\n'
            '      "expected_output": "What the output should look like",\n'
            '      "expectations": ["expectation statements"],\n'
            '      "assertions": [{"name": "...", "type": "ASSERTION_TYPE", "params": {...}}],\n'
            '      "fixtures": [{"path": "relative/path", "content": "file contents"}]\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "Generate 8-15 eval cases total, spread across the different domains.\n"
            "Output ONLY the JSON, no other text.\n\n"
            f"## Skill Definition\n\n"
            f"Skill name: {name}\n"
            f"Description: {description}\n\n"
            f"Full SKILL.md content:\n{content[:10000]}\n"
        )

    def _format_samples(self, samples: list[dict]) -> str:
        if not samples:
            return "(No sample files available)"
        parts = []
        for s in samples[:8]:
            content = s.get("content", "")
            if len(content) > 2000:
                content = content[:2000] + "\n...(truncated)"
            parts.append(f"### {s.get('path', 'unknown')}\n```\n{content}\n```")
        return "\n".join(parts)

    def _parse_response(self, response: str, fallback_name: str) -> EvalSet | None:
        if not response:
            return None
        try:
            data = json.loads(response)
            if "evals" in data:
                return EvalSet.model_validate(data)
        except (json.JSONDecodeError, Exception):
            pass

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if "evals" in data:
                    return EvalSet.model_validate(data)
            except Exception:
                pass

        return None

    def _fallback_eval_set(self, name: str, description: str) -> EvalSet:
        return EvalSet(
            skill_name=name,
            evals=[
                EvalCase(
                    id=1,
                    name="L1-general-comprehension",
                    prompt=f"Describe what the {name} skill does and when to use it",
                    level="L1",
                    expectations=[
                        f"Mentions the core purpose of the {name} skill",
                        "Lists key capabilities or features",
                    ],
                    assertions=[],
                ),
                EvalCase(
                    id=2,
                    name="L2-general-scenario",
                    prompt=f"Use the {name} skill to complete the task described in input.txt",
                    level="L2",
                    expected_output=f"Correct output as specified by the {name} skill",
                    expectations=[f"Output follows {name} skill rules"],
                    assertions=[],
                    fixtures=[{
                        "path": "input.txt",
                        "content": f"A general-purpose task that exercises the {name} skill",
                    }],
                ),
            ],
        )
