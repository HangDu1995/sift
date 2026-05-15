"""Eval init generator — creates evals.json template for a Skill."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from skill_eval.models import Assertion, AssertionTier, AssertionType, EvalCase, EvalSet
from skill_eval.parser import parse_skill_md
from skill_eval.utils import call_claude

if TYPE_CHECKING:
    from skill_eval.config.loader import EvalConfig

MAX_EVALS = 50
MAX_EXPECTATIONS_PER_EVAL = 8


class EvalInitGenerator:
    def __init__(
        self,
        model: str | None = None,
        timeout: int = 120,
        config: EvalConfig | None = None,
        modes: list[str] | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.config = config
        self.modes = modes or []

    def generate_template(self, skill_path: Path) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        effective_modes = self.modes or frontmatter.get("modes", [])
        domain = frontmatter.get("domain", "general")

        cases: list[EvalCase] = []
        sections = self._split_into_sections(body)

        eval_id = 0
        for section_title, section_text in sections:
            if not section_text.strip():
                continue

            prompt = self._derive_prompt(section_title, section_text, name, description)
            expectations = self._derive_expectations(section_text, name)
            base_assertions = self._derive_assertions(section_text)
            domain_assertions = self._derive_domain_assertions(domain)
            assertions = self._merge_assertions(base_assertions, domain_assertions)
            expected_output = self._derive_expected_output(section_title, section_text, description)

            if effective_modes:
                for mode in effective_modes:
                    eval_id += 1
                    cases.append(EvalCase(
                        id=eval_id,
                        name=f"{section_title} [{mode}]",
                        prompt=prompt,
                        expected_output=expected_output,
                        expectations=expectations,
                        assertions=assertions,
                        mode=mode,
                    ))
            else:
                eval_id += 1
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

        for case in cases:
            case.assertions = self._auto_generate_assertions(case)

        return EvalSet(skill_name=name, evals=cases)

    def generate_with_llm(self, skill_path: Path) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        prompt = self._build_llm_prompt(name, description, content)
        try:
            response = call_claude(prompt, model=self.model, timeout=self.timeout)
        except RuntimeError:
            return self.generate_template(skill_path)

        eval_set = self._parse_llm_response(response, name)
        if eval_set is not None:
            for case in eval_set.evals:
                case.assertions = self._auto_generate_assertions(case)
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

        items = self._extract_list_items(text)
        context_hint = ""
        if items:
            first_item = re.sub(r"\*\*|\[.*?\]\(.*?\)|`", "", items[0]).strip()
            if first_item:
                context_hint = f" — Specifically, {first_item[:80]}"

        action = self._detect_action(title_clean + " " + description)
        context = self._first_sentence(description) or skill_name
        return f"{action} a task related to {title_clean} — {context}{context_hint}"

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
            expectation = self._item_to_expectation(clean)
            expectations.append(expectation)
            if len(expectations) >= MAX_EXPECTATIONS_PER_EVAL:
                break

        if not expectations:
            expectations.append(f"The output demonstrates understanding of the {skill_name} skill rules and produces a relevant response")

        return expectations

    def _item_to_expectation(self, item: str) -> str:
        lower = item.lower()

        file_refs = re.findall(r'[\w/\-]+\.\w+', item)
        if file_refs:
            return f"Output references or produces content related to: {', '.join(file_refs[:3])}"

        if any(kw in lower for kw in ['检查', '验证', 'check', 'verify', 'validate', 'ensure']):
            return f"The response addresses verification: {item[:80]}"

        if any(kw in lower for kw in ['生成', '创建', 'generate', 'create', 'produce', 'output']):
            return f"The response generates output that: {item[:80]}"

        if any(kw in lower for kw in ['询问', '提示', 'ask', 'prompt', 'request']):
            return f"The response includes a user-facing question or prompt: {item[:80]}"

        if any(kw in lower for kw in ['应该', '必须', 'should', 'must', 'need', 'require']):
            return f"The response follows the rule: {item[:80]}"

        if '→' in item or '->' in item:
            parts = re.split(r'[→\-]+|->+', item, maxsplit=1)
            if len(parts) == 2 and parts[1].strip():
                return f"When {parts[0].strip()[:60]}, the output should {parts[1].strip()[:60]}"

        return f"The response addresses this aspect: {item[:100]}"

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

    def _derive_domain_assertions(self, domain: str) -> list[Assertion]:
        if not self.config:
            return []
        templates = self.config.domain_templates.get(domain, [])
        result: list[Assertion] = []
        for t in templates:
            atype_str = t.get("type", "")
            try:
                atype = AssertionType(atype_str)
            except ValueError:
                continue
            params = {k: v for k, v in t.items() if k not in ("type", "description")}
            result.append(Assertion(
                name=t.get("description", f"Domain check: {atype_str}"),
                type=atype,
                params=params,
            ))
        return result

    @staticmethod
    def _merge_assertions(base: list[Assertion], extra: list[Assertion]) -> list[Assertion]:
        seen = {(a.type, json.dumps(a.params, sort_keys=True)) for a in base}
        merged = list(base)
        for a in extra:
            key = (a.type, json.dumps(a.params, sort_keys=True))
            if key not in seen:
                seen.add(key)
                merged.append(a)
        return merged

    @staticmethod
    def _auto_generate_assertions(eval_case: EvalCase) -> list[Assertion]:
        assertions: list[Assertion] = list(eval_case.assertions)
        seen = {(a.type, json.dumps(a.params, sort_keys=True)) for a in assertions}

        def _add(a: Assertion) -> None:
            key = (a.type, json.dumps(a.params, sort_keys=True))
            if key not in seen:
                seen.add(key)
                assertions.append(a)

        # From fixtures: every fixture file → FILE_EXISTS (Tier 1)
        for f in eval_case.fixtures:
            if f.path:
                _add(Assertion(
                    name=f"文件 {f.path} 存在",
                    type=AssertionType.FILE_EXISTS,
                    params={"filename": f.path},
                    tier=AssertionTier.TIER1,
                ))

        # From expectations: extract file names, patterns, negative patterns
        for exp in eval_case.expectations:
            exp_lower = exp.lower()

            # File references: "生成 X.md", "写入 Y.json", "输出 Z.txt" → Tier 1
            file_refs = re.findall(r'[\w/\-]+\.(?:md|json|yaml|yml|xml|txt|ets|ts|py|js|csv|html)', exp)
            for fr in file_refs[:2]:
                _add(Assertion(
                    name=f"产出文件 {fr}",
                    type=AssertionType.FILE_EXISTS,
                    params={"filename": fr},
                    tier=AssertionTier.TIER1,
                ))

            # "包含 X" / "提到 Y" → Tier 1
            contains_patterns = re.findall(r'[包含提及展示输出写出].{0,3}["「《]([^"」》]{3,40})["」》]', exp)
            if not contains_patterns:
                contains_patterns = re.findall(r'(?:includes|contains|mentions|shows|outputs)\s+["\u201c]([^"\u201d]{3,40})["\u201d]', exp_lower)
            if not contains_patterns:
                keywords = re.findall(r'(?:验证|检查|确认).{0,3}([A-Za-z_\./]{3,40})', exp)
                if not keywords:
                    keywords = re.findall(r'([A-Z][a-z]+(?:[A-Z][a-z]+){2,})', exp)
                    if not keywords:
                        keywords = re.findall(r'([A-Z][a-z]+(?:Layout|Activity|Fragment|Manager|Service|View))', exp)
                if keywords:
                    contains_patterns = keywords
            for kw in contains_patterns[:2]:
                if len(kw) >= 3:
                    _add(Assertion(
                        name=f"输出包含: {kw}",
                        type=AssertionType.CONTAINS_PATTERN,
                        params={"pattern": re.escape(kw)},
                        tier=AssertionTier.TIER1,
                    ))

            # "不应 X" / "不能 Y" → Tier 1
            not_patterns = re.findall(r'(?:不应|不能|禁止|避免|不可|不得).{0,5}["\u201c]([^"\u201d]{3,40})["\u201d]', exp)
            for kw in not_patterns[:1]:
                _add(Assertion(
                    name=f"输出不应包含: {kw}",
                    type=AssertionType.NOT_CONTAINS,
                    params={"pattern": re.escape(kw)},
                    tier=AssertionTier.TIER1,
                ))

            # "委托" / "delegate" → Tier 3 LLM_NOT_DELEGATE
            if any(kw in exp_lower for kw in ['委托', 'delegate', '调度', 'dispatch']):
                target_match = re.search(r'(?:委托|调度).{0,5}["\u201c]?([a-zA-Z][a-zA-Z0-9_-]{2,30})', exp, re.IGNORECASE)
                if target_match:
                    _add(Assertion(
                        name=f"未委托给 {target_match.group(1)}",
                        type=AssertionType.LLM_NOT_DELEGATE,
                        params={"target_skill": target_match.group(1)},
                        tier=AssertionTier.TIER3,
                        retry_on_uneval=True,
                    ))

            # Gate-related → Tier 2
            if any(kw in exp_lower for kw in ['审批', '确认', 'gate', '门禁', '等待']):
                _add(Assertion(
                    name="请求用户确认",
                    type=AssertionType.GATE_REQUESTED,
                    params={},
                    tier=AssertionTier.TIER2,
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
            '      "prompt": "A specific, targeted question/scenario that tests ONE rule or behavior. '
            'For multi-step pipeline skills, provide enough context (e.g. project structure, file contents) '
            'so the model can complete the task autonomously.",\n'
            '      "expected_output": "What the correct response looks like",\n'
            '      "expectations": ["CRITICAL: Each expectation must be a SINGLE, CONCRETE, VERIFIABLE statement '
            'that can be objectively checked. GOOD: \'Output contains a file path matching pattern *.md\'. '
            'GOOD: \'Response mentions checking for AndroidManifest.xml\'. '
            'GOOD: \'Output includes a numbered list of steps\'. '
            'BAD: \'The output follows the skill instructions\'. '
            'BAD: \'The response is correct and complete\'."],\n'
            '      "assertions": [\n'
            '        {"name": "File X exists", "type": "FILE_EXISTS", "params": {"filename": "path/to/file"}},\n'
            '        {"name": "Output contains pattern Y", "type": "CONTAINS_PATTERN", "params": {"pattern": "regex"}},\n'
            '        {"name": "JSON field Z present", "type": "JSON_HAS_FIELD", "params": {"filename": "f.json", "field": "key"}}\n'
            '      ],\n'
            '      "fixtures": [{"path": "relative/path/file.ext", "content": "file content"}]\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Critical Rules for Expectations\n\n"
            "- NEVER write vague expectations like 'The output follows the skill instructions without errors'\n"
            "- EVERY expectation must be checkable by examining the output text or files produced\n"
            "- Use concrete patterns: 'contains X', 'mentions Y', 'includes a section titled Z', "
            "'produces a file named W', 'lists at least N items'\n"
            "- For pipeline skills with multiple phases, test ONE phase per eval case\n\n"
            "## Critical Rules for Assertions\n\n"
            "Generate 2-5 tiered assertions for EVERY eval case. Assertions are organized into three tiers:\n\n"
            "**Tier 1 (Deterministic)**: FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS, JSON_HAS_FIELD, "
            "LINE_COUNT, TOOL_CALLED, STEP_COUNT. Always include at least 1 Tier 1.\n"
            "  - FILE_EXISTS: {\"name\": \"spec file exists\", \"type\": \"FILE_EXISTS\", \"tier\": \"TIER1\", "
            "\"params\": {\"filename\": \"spec/baseline/ui-manifest.md\"}}\n"
            "  - CONTAINS_PATTERN: {\"name\": \"mentions page_id\", \"type\": \"CONTAINS_PATTERN\", "
            "\"tier\": \"TIER1\", \"params\": {\"pattern\": \"page_id\"}}\n\n"
            "**Tier 2 (Behavioral)**: GATE_REQUESTED, GATE_NOT_SKIPPED, PHASE_ORDERED, FILE_NOT_WRITTEN. "
            "Use when testing procedural gates and ordering.\n"
            "  - GATE_REQUESTED: {\"name\": \"gate B requested\", \"type\": \"GATE_REQUESTED\", "
            "\"tier\": \"TIER2\", \"params\": {\"gate\": \"Phase B\"}}\n\n"
            "**Tier 3 (Semantic)**: LLM_GRADE, LLM_FACT, LLM_NOT_DELEGATE. "
            "For quality judgments and delegation checks. Always use retry_on_uneval: true.\n"
            "  - LLM_NOT_DELEGATE: {\"name\": \"no <TARGET_SKILL> delegation\", "
            "\"type\": \"LLM_NOT_DELEGATE\", \"tier\": \"TIER3\", \"retry_on_uneval\": true, "
            "\"params\": {\"target_skill\": \"<TARGET_SKILL>\"}}\n"
            "**CRITICAL**: NEVER put a CONTAINS_PATTERN/NOT_CONTAINS and an expectation "
            "checking the same semantic thing. If you need to verify delegation to a skill, "
            "use LLM_NOT_DELEGATE (Tier 3) — NOT CONTAINS_PATTERN or NOT_CONTAINS. "
            "Programmatic assertions and expectations must not overlap.\n\n"
            "## Other Guidelines\n\n"
            "- **Prompt specificity**: Each prompt should target ONE specific rule or behavior, not the entire skill\n"
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
