"""Eval init generator — creates evals.json template for a Skill."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from skill_eval.models import Assertion, AssertionType, EvalCase, EvalSet
from skill_eval.parser import parse_skill_md

MAX_EVALS = 50
MAX_EXPECTATIONS_PER_EVAL = 8


class EvalInitGenerator:
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def generate_template(self, skill_path: Path) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        cases: list[EvalCase] = []
        sections = self._split_into_sections(body)

        eval_id = 0
        for section_title, section_text in sections:
            if not section_text.strip():
                continue

            eval_id += 1
            prompt = self._derive_prompt(section_title, section_text, name, description)
            expectations = self._derive_expectations(section_text, name)
            assertions = self._derive_assertions(section_text)
            expected_output = self._derive_expected_output(section_title, section_text, description)

            cases.append(EvalCase(
                id=eval_id,
                name=section_title,
                prompt=prompt,
                expected_output=expected_output,
                expectations=expectations,
                assertions=assertions,
            ))

            if len(cases) >= MAX_EVALS:
                break

        if not cases:
            cases.append(EvalCase(
                id=1,
                name="basic",
                prompt=self._build_fallback_prompt(name, description),
                expected_output=f"Correct application of the {name} skill",
                expectations=[f"The output demonstrates correct use of the {name} skill"],
                assertions=[],
            ))

        return EvalSet(skill_name=name, evals=cases)

    def generate_with_llm(self, skill_path: Path) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        prompt = self._build_llm_prompt(name, description, content)
        response = self._call_claude(prompt)

        eval_set = self._parse_llm_response(response, name)
        if eval_set is not None:
            return eval_set

        return self.generate_template(skill_path)

    # ------------------------------------------------------------------
    # Section splitting — split on ## headings only, fold ###+ in
    # ------------------------------------------------------------------

    def _split_into_sections(self, body: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_title = ""
        current_lines: list[str] = []

        for line in body.split("\n"):
            if line.startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                if level > 2:
                    current_lines.append(line)
                    continue
                if current_lines:
                    sections.append((current_title, "\n".join(current_lines)))
                current_title = line.lstrip("#").strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                sections.append((current_title or "Overview", text))

        return sections

    # ------------------------------------------------------------------
    # Prompt derivation — language-agnostic
    # ------------------------------------------------------------------

    def _derive_prompt(self, title: str, text: str, skill_name: str, description: str) -> str:
        title_clean = title.strip()
        if not title_clean:
            return self._build_fallback_prompt(skill_name, description)

        action = self._detect_action(title_clean + " " + description)
        context = self._first_sentence(description) or skill_name
        return f"{action} a task related to {title_clean} — {context}"

    def _detect_action(self, text: str) -> str:
        lower = text.lower()
        action_keywords = [
            ("convert", "Convert"),
            ("transform", "Transform"),
            ("migrate", "Migrate"),
            ("refactor", "Refactor"),
            ("create", "Create"),
            ("generate", "Generate"),
            ("build", "Build"),
            ("implement", "Implement"),
            ("write", "Write"),
            ("extract", "Extract"),
            ("analyze", "Analyze"),
            ("debug", "Debug"),
            ("fix", "Fix"),
            ("optimize", "Optimize"),
            ("validate", "Validate"),
            ("deploy", "Deploy"),
            ("process", "Process"),
            ("execute", "Execute"),
            ("run", "Run"),
            ("handle", "Handle"),
            ("configure", "Configure"),
            ("manage", "Manage"),
            ("design", "Design"),
            ("review", "Review"),
            ("test", "Test"),
        ]
        for keyword, verb in action_keywords:
            if keyword in lower:
                return verb
        return "Use"

    def _first_sentence(self, text: str) -> str:
        if not text:
            return ""
        for sep in (".", "。", "\n"):
            idx = text.find(sep)
            if idx > 0:
                return text[:idx + 1].strip()
        return text[:100].strip()

    def _build_fallback_prompt(self, skill_name: str, description: str) -> str:
        context = self._first_sentence(description) or skill_name
        return f"Use the {skill_name} skill to accomplish a task — {context}"

    # ------------------------------------------------------------------
    # Expected output
    # ------------------------------------------------------------------

    def _derive_expected_output(self, title: str, text: str, description: str) -> str:
        steps = self._extract_list_items(text)
        if steps:
            step_count = len(steps)
            return f"Successfully complete the {title} workflow ({step_count} steps) as defined in the Skill"
        return f"Successfully complete the {title} task as defined in the Skill"

    # ------------------------------------------------------------------
    # Expectation derivation — language-agnostic
    # ------------------------------------------------------------------

    def _derive_expectations(self, text: str, skill_name: str) -> list[str]:
        items = self._extract_list_items(text)
        expectations: list[str] = []

        for item in items:
            clean = re.sub(r"\*\*|\[.*?\]\(.*?\)|`", "", item).strip()
            if len(clean) < 5:
                continue
            expectations.append(clean)
            if len(expectations) >= MAX_EXPECTATIONS_PER_EVAL:
                break

        if not expectations:
            expectations.append(f"The output follows the {skill_name} skill instructions without errors")

        return expectations

    def _extract_list_items(self, text: str) -> list[str]:
        items: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            m = re.match(r"^[*\-]\s+(.+)", stripped)
            if m:
                items.append(m.group(1).strip())
                continue
            m = re.match(r"^\d+\.\s+(.+)", stripped)
            if m:
                items.append(m.group(1).strip())
        return items

    # ------------------------------------------------------------------
    # Assertion derivation
    # ------------------------------------------------------------------

    def _derive_assertions(self, text: str) -> list[Assertion]:
        assertions: list[Assertion] = []
        seen_names: set[str] = set()

        code_blocks = re.findall(r"```[\w]*\n(.*?)```", text, re.DOTALL)
        for block in code_blocks[:3]:
            filenames = re.findall(
                r"(?:open|write|create|read|load|save)\s*[\(\s]['\"]?([\w\-\.]+\.\w+)['\"]?",
                block,
            )
            for fn in set(filenames):
                if fn not in seen_names:
                    seen_names.add(fn)
                    assertions.append(Assertion(
                        name=f"File {fn} exists",
                        type=AssertionType.FILE_EXISTS,
                        params={"filename": fn},
                    ))

        if re.search(r"json\.(dump|load|dumps|loads)|JSON\.(stringify|parse)", text):
            assertions.append(Assertion(
                name="Output contains JSON structure",
                type=AssertionType.CONTAINS_PATTERN,
                params={"pattern": r"[{[]"},
            ))

        imports = re.findall(r"import\s+([\w.]+)", text)
        for imp in imports[:2]:
            mod = imp.split(".")[0]
            if mod not in seen_names:
                seen_names.add(mod)
                assertions.append(Assertion(
                    name=f"Uses {mod}",
                    type=AssertionType.CONTAINS_PATTERN,
                    params={"pattern": mod},
                ))

        return assertions

    # ------------------------------------------------------------------
    # LLM-based generation
    # ------------------------------------------------------------------

    def _build_llm_prompt(self, name: str, description: str, content: str) -> str:
        return (
            "You are an expert at creating evaluation test cases for AI coding assistant skills.\n\n"
            "Given the following Skill definition, generate **L1 unit-level** evaluation test cases.\n\n"
            "## L1 Unit-Level Test Definition\n\n"
            "Each test case tests whether Claude **understands and can apply** the skill's rules.\n"
            "L1 tests do NOT require a real project — they test rule comprehension via targeted prompts.\n\n"
            "## Test Case Categories\n\n"
            "Generate test cases for these categories (pick the most relevant ones for this skill):\n\n"
            "1. **Input Validation**: What happens when preconditions are met/not met?\n"
            "2. **Decision Logic**: Does Claude follow the correct branching/rules for different scenarios?\n"
            "3. **Scheduling/Ordering**: Does Claude understand execution order, parallelism, dependencies?\n"
            "4. **Skill Binding**: Does Claude know which sub-skills to use in which context?\n"
            "5. **Error Handling**: Does Claude follow failure recovery/fallback rules?\n"
            "6. **Output Format**: Does Claude produce output in the required format/structure?\n\n"
            "## Schema\n\n"
            "Output a JSON object matching this exact schema:\n"
            "```json\n"
            "{\n"
            f'  "skill_name": "{name}",\n'
            '  "evals": [\n'
            "    {\n"
            '      "id": 1,\n'
            '      "name": "L1-<category>-<short description>",\n'
            '      "level": "L1",\n'
            '      "prompt": "A specific, targeted question/scenario that tests ONE rule or behavior",\n'
            '      "expected_output": "What the correct response looks like",\n'
            '      "expectations": ["Specific, verifiable statements about what Claude should say/do"],\n'
            '      "assertions": [],\n'
            '      "fixtures": [{"path": "relative/path/file.ext", "content": "file content"}]\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Guidelines\n\n"
            "- **Prompt specificity**: Each prompt should target ONE specific rule or behavior, not the entire skill\n"
            "- **Expectations verifiability**: Each expectation must be a concrete statement that can be "
            "checked in Claude's transcript (e.g., 'mentions X', 'recommends Y', 'does NOT suggest Z')\n"
            "- **Fixtures**: Only include fixtures when the test requires Claude to check file existence/content. "
            "Most L1 tests need NO fixtures (empty array).\n"
            "- **Language**: Write prompts in the SAME language as the SKILL.md. "
            "If SKILL.md is in Chinese, write prompts in Chinese.\n"
            "- **Coverage**: Aim for 5-10 test cases covering the most important rules\n"
            "- **Edge cases**: Include at least one negative test (wrong input, missing precondition)\n"
            "- Output ONLY the JSON, no other text\n\n"
            f"## Skill Definition\n\n"
            f"Skill name: {name}\n"
            f"Description: {description}\n\n"
            f"Full SKILL.md content:\n{content[:10000]}\n"
        )

    def _parse_llm_response(self, response: str, fallback_name: str) -> EvalSet | None:
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

    def _call_claude(self, prompt: str) -> str:
        cmd = ["claude", "-p", "--output-format", "text", "--dangerously-skip-permissions"]
        if self.model:
            cmd.extend(["--model", self.model])
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            env=env, timeout=self.timeout,
        )
        return result.stdout
