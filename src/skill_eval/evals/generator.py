"""Eval init generator — creates evals.json template for a Skill."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from skill_eval.errors import SkillEvalError
from skill_eval.models import Assertion, AssertionTier, AssertionType, EvalCase, EvalSet
from skill_eval.parser import parse_skill_md
from skill_eval.utils import call_claude

# mining 心跳间隔(秒):generate_from_transcript 单次慢 LLM 调用期间每 N 秒报一次仍在跑。
# 模块常量便于测试 monkeypatch 成极小值验心跳触发。
_HEARTBEAT_INTERVAL = 20

# 方向 B:从 SKILL.md 确定性抽【产物】文件名,把 skill 声明的产物并入 mining manifest。
# 原则性客观断言源自 skill、transcript 无关 —— 违规 transcript 没产出 skill 声明的产物时,
# 也不该让它的 FILE_EXISTS 被 closed-set manifest drop。
_SKILL_OUTPUT_SECTION_RE = re.compile(
    r"(输出|output|产物|产出|生成物|交付物|deliverable|result|结果)", re.IGNORECASE)
_SKILL_STEP_SECTION_RE = re.compile(
    r"(步骤|step|流程|workflow|procedure|过程)", re.IGNORECASE)
# 写/产出动词(锚定产物);读动词(读取/read)锚定的是输入,不在此列
_SKILL_WRITE_VERB_RE = re.compile(
    r"(写出|写入|写至|写到|生成|创建|输出|产出|保存|导出|emit|write\s+(?:out|to)|"
    r"create|generate|produce|output|save|export)", re.IGNORECASE)
_SKILL_FILENAME_RE = re.compile(r"[\w/\-]+\.[\w]+")

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
        is_procedural = self._is_procedural_skill(content, skill_path)

        prompt = self._build_llm_prompt(name, description, content, is_procedural)
        try:
            response = call_claude(prompt, model=self.model, timeout=self.timeout)
        except RuntimeError:
            return self.generate_template(skill_path)

        eval_set = self._parse_llm_response(response, name)
        if eval_set is not None:
            for case in eval_set.evals:
                if case.level == "L1":
                    case.assertions = self._auto_generate_assertions(case)
            return eval_set

        return self.generate_template(skill_path)

    def _is_procedural_skill(self, content: str, skill_path: Path) -> bool:
        """Detect if skill is tool-chain/procedural (deserves L2 evals)."""
        code_blocks = len(re.findall(r'```', content)) // 2
        has_scripts = (skill_path / "scripts").is_dir()
        has_references = (skill_path / "references").is_dir()
        return code_blocks >= 3 or has_scripts or has_references

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
        """Auto-derive assertions from expectations. LLM-generated take priority."""
        # If LLM already produced sufficient assertions, use them as-is
        if eval_case.assertions and len(eval_case.assertions) >= len(eval_case.expectations):
            return eval_case.assertions

        assertions: list[Assertion] = list(eval_case.assertions)
        seen = {(a.type, json.dumps(a.params, sort_keys=True)) for a in assertions}
        is_l1 = eval_case.level == "L1"

        def _add(a: Assertion) -> None:
            key = (a.type, json.dumps(a.params, sort_keys=True))
            if key not in seen:
                seen.add(key)
                assertions.append(a)

        # From expectations: extract patterns
        for exp in eval_case.expectations:
            exp_lower = exp.lower()

            # File references: only add FILE_EXISTS for L2 evals (L1 doesn't produce files)
            if not is_l1:
                file_refs = re.findall(r'[\w/\-]+\.(?:md|json|yaml|yml|xml|txt|py|js|csv|html|pdf)', exp)
                for fr in file_refs[:2]:
                    _add(Assertion(name=f"产出文件 {fr}", type=AssertionType.FILE_EXISTS,
                                   params={"filename": fr}))

            # "包含 X" / "mentions Y" → Tier 1
            for kw in re.findall(r'["\u201c]([^"\u201d]{3,50})["\u201d]', exp):
                _add(Assertion(name=f"输出包含: {kw}", type=AssertionType.CONTAINS_PATTERN,
                               params={"pattern": re.escape(kw)}))
            for kw in re.findall(r'(?:includes|contains|mentions)\s+["\u201c]([^"\u201d]{3,50})["\u201d]', exp_lower):
                _add(Assertion(name=f"输出包含: {kw}", type=AssertionType.CONTAINS_PATTERN,
                               params={"pattern": re.escape(kw)}))

            # "不应 X" / "does not contain Y" → Tier 1
            for kw in re.findall(r'(?:不应|不能|禁止|避免|不包含|does not|should not)\s*["\u201c]?([^"\u201d\s]{3,50})["\u201d]?', exp, re.IGNORECASE):
                _add(Assertion(name=f"输出不含: {kw}", type=AssertionType.NOT_CONTAINS,
                               params={"pattern": re.escape(kw)}))

            # "委托" / "delegate" → Tier 3
            if any(kw in exp_lower for kw in ['委托', 'delegate', '调度', 'dispatch']):
                m = re.search(r'(["a-zA-Z][a-zA-Z0-9_-]{2,30})', exp)
                if m:
                    _add(Assertion(name=f"未委托给 {m.group(1)}", type=AssertionType.LLM_NOT_DELEGATE,
                                   params={"target_skill": m.group(1)}, tier=AssertionTier.TIER3,
                                   retry_on_uneval=True))

        return assertions

    @staticmethod
    def _enforce_manifest(eval_set: EvalSet, manifest: set[str]) -> EvalSet:
        """确定性后处理:把裸名补全成 manifest 路径、scoped path、CUSTOM_SCRIPT↔fixture 配对;
        解析不了的断言 drop 并在 eval.flags 标记。"""
        # basename -> full(仅当唯一)
        by_base: dict[str, str] = {}
        ambig: set[str] = set()
        for p in manifest:
            b = os.path.basename(p)
            if b in by_base:
                ambig.add(b)
            else:
                by_base[b] = p
        for b in ambig:
            by_base.pop(b, None)

        def resolve(raw: str) -> str | None:
            if not raw:
                return None
            if raw in manifest:
                return raw
            base = os.path.basename(raw)
            return by_base.get(base)

        for ev in eval_set.evals:
            fixture_paths = {f.path for f in ev.fixtures}
            kept: list[Assertion] = []
            dropped: list[str] = []
            for a in ev.assertions:
                p = a.params
                if a.type in (AssertionType.FILE_EXISTS, AssertionType.JSON_HAS_FIELD):
                    fn = resolve(p.get("filename", ""))
                    if fn:
                        p["filename"] = fn
                        kept.append(a)
                    else:
                        dropped.append(a.name)
                elif a.type in (AssertionType.CONTAINS_PATTERN, AssertionType.NOT_CONTAINS):
                    raw_path = p.get("path", "")
                    if not raw_path:
                        kept.append(a)          # path-less 合法(scorer rglob+transcript),L1 用
                    else:
                        path = resolve(raw_path)
                        if path:
                            p["path"] = path
                            kept.append(a)
                        else:
                            dropped.append(a.name)
                elif a.type == AssertionType.CUSTOM_SCRIPT:
                    script = p.get("script", "")
                    resolved = resolve(script)
                    if (resolved or script) in fixture_paths:
                        p["script"] = resolved or script
                        kept.append(a)
                    else:
                        dropped.append(f"{a.name}(unpaired checker)")
                else:
                    kept.append(a)
            ev.assertions = kept
            for f in ev.fixtures:
                if f.path not in manifest and resolve(f.path) is None:
                    ev.flags.append(f"review:fixture-not-in-manifest:{f.path}")
            if dropped:
                ev.flags.append("review:dropped-assertions:" + ",".join(dropped))
        return eval_set

    # ------------------------------------------------------------------
    # LLM-based generation
    # ------------------------------------------------------------------

    def _build_llm_prompt(self, name: str, description: str, content: str, is_procedural: bool = False) -> str:
        if is_procedural:
            return self._build_procedural_prompt(name, description, content)
        return self._build_knowledge_prompt(name, description, content)

    def _build_knowledge_prompt(self, name: str, description: str, content: str) -> str:
        return (
            "You are an expert at creating evaluation test cases for AI coding assistant skills.\n\n"
            "Given the following Skill definition, generate **L1 rule-comprehension** evaluation test cases.\n"
            "L1 tests check whether Claude **understands the rules and conventions** — NOT whether it can execute tasks.\n\n"
            "## Categories (pick the most relevant)\n\n"
            "1. **Decision Logic**: branching rules, conditions, scenarios\n"
            "2. **Input Validation**: what happens when preconditions met/not met?\n"
            "3. **Output Format**: does Claude know the required output structure?\n"
            "4. **Skill Binding**: which sub-skill/tool to use in which context?\n"
            "5. **Error Handling**: failure recovery and fallback rules\n"
            "6. **Constraint Following**: NEVER/ALWAYS/MUST rules\n\n"
            "## Output JSON (ONLY this, no other text)\n"
            "```json\n"
            "{\n"
            f'  "skill_name": "{name}",\n'
            '  "evals": [\n'
            "    {\n"
            '      "id": 1,\n'
            '      "name": "L1-<category>-<short>",\n'
            '      "level": "L1",\n'
            '      "prompt": "A specific question that tests ONE rule. '
            'Make it realistic — what would a real user ask?",\n'
            '      "expectations": ["Single, concrete, verifiable claim. '
            'GOOD: \'response mentions checking for AndroidManifest.xml\'. '
            'BAD: \'the output is correct\'. BAD: \'follows the skill instructions\'."],\n'
            '      "assertions": [\n'
            '        {"name": "mentions X", "type": "CONTAINS_PATTERN", "params": {"pattern": "keyword"}},\n'
            '        {"name": "avoids Y", "type": "NOT_CONTAINS", "params": {"pattern": "forbidden"}}\n'
            "      ],\n"
            '      "fixtures": []\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Rules\n"
            "- 5-10 evals covering the most important rules\n"
            "- NO vague expectations — every one must be objectively checkable\n"
            "- assertions use ONLY: CONTAINS_PATTERN, NOT_CONTAINS for text checks. "
            "NO FILE_EXISTS — L1 tests don't produce files.\n"
            "- Language: same as SKILL.md\n"
            "- At least 1 negative test (wrong input, missing precondition)\n\n"
            f"## Skill\n\nname: {name}\ndescription: {description}\n\n{content[:10000]}\n"
        )

    def _build_procedural_prompt(self, name: str, description: str, content: str) -> str:
        return (
            "You are an expert at creating evaluation test cases for AI coding assistant skills.\n\n"
            "This skill contains executable procedures (code examples, scripts, or reference files). "
            "Generate a mix of L1 and L2 test cases.\n\n"
            "## L1 Tests (40% — ~3-4 cases)\n"
            "Test whether Claude **understands the rules**: which tool to use, what parameters, "
            "what order to do things, what NOT to do. Prompt asks a question or describes a scenario "
            "and expects a knowledgeable response. NO fixtures needed.\n\n"
            "## L2 Tests (60% — ~4-6 cases)\n"
            "Test whether Claude **actually executes** the task correctly. "
            "Provide fixture files as input and check output files with assertions. "
            "These test the exact code patterns and workflows from the skill.\n\n"
            "For L2, include fixtures that simulate real input data. "
            "For example, if the skill processes PDF files, provide a minimal valid fixture. "
            "Use FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS assertions to check outputs.\n\n"
            "## Output JSON (ONLY this, no other text)\n"
            "```json\n"
            "{\n"
            f'  "skill_name": "{name}",\n'
            '  "evals": [\n'
            "    {\n"
            '      "id": 1,\n'
            '      "name": "L1-<category>-<short>",\n'
            '      "level": "L1",\n'
            '      "prompt": "Question/scenario testing knowledge of ONE rule",\n'
            '      "expectations": ["Verifiable claim about what Claude should say/know"],\n'
            '      "assertions": [\n'
            '        {"name": "mentions correct tool", "type": "CONTAINS_PATTERN", "params": {"pattern": "keyword"}}\n'
            '      ],\n'
            '      "fixtures": []\n'
            "    },\n"
            "    {\n"
            '      "id": 5,\n'
            '      "name": "L2-<operation>-<scenario>",\n'
            '      "level": "L2",\n'
            '      "prompt": "A realistic task: process/generate/convert file X, produce output Y. '
            'Include the exact file paths from fixtures.",\n'
            '      "expectations": ["Verifiable claim about the OUTPUT FILES produced — '
            'e.g. \'generated merged.pdf with 3 pages\', \'extracted text saved to output.txt\'"],\n'
            '      "assertions": [\n'
            '        {"name": "output file exists", "type": "FILE_EXISTS", "params": {"filename": "output.pdf"}},\n'
            '        {"name": "contains expected content", "type": "CONTAINS_PATTERN", "params": {"pattern": "Expected", "path": "output.txt"}}\n'
            "      ],\n"
            '      "fixtures": [\n'
            '        {"path": "input.pdf", "content": "(simulated file content — make it realistic but minimal)"}\n'
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Rules\n"
            "- Total 8-12 evals\n"
            "- L1: test rules/decisions (no fixtures). L2: test actual execution (with fixtures).\n"
            "- L2 fixtures must be REALISTIC minimal test data that Claude can actually process.\n"
            "- For text-based fixtures (JSON/code), include the ACTUAL content. "
            "For binary fixtures (PDF/image), write a minimal placeholder.\n"
            "- L2 expectations must mention CONCRETE output: file names, counts, patterns.\n"
            "- L1 assertions: CONTAINS_PATTERN, NOT_CONTAINS only. NO FILE_EXISTS on L1.\n"
            "- L2 assertions: FILE_EXISTS, CONTAINS_PATTERN, NOT_CONTAINS, JSON_HAS_FIELD.\n"
            "- Language: same as SKILL.md\n"
            "- Include at least 1 error-handling or edge-case scenario\n\n"
            f"## Skill\n\nname: {name}\ndescription: {description}\n\n{content[:8000]}\n"
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

    # ------------------------------------------------------------------
    # Transcript-driven generation
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_same_task_evals(eval_set: EvalSet) -> EvalSet:
        """合并 prompt 相同的 eval → 一题多断言(去冗余、省 spawn)。

        单 transcript 挖掘常产出多个同 prompt 的 eval(同一任务的多条评分细则:认某错误码、
        schema 完整、反编造…),各自 spawn 一次 cc 冗余、A/B 时是高度相关 trial。按 prompt
        分组合并:assertions / expectations / fixtures 取并集去重、expected_output 拼成 rubric、
        level 取有 fixture→L2。无同 prompt 则原样返回(仅重排 id)。flag 标 ``merged:N-evals``
        (不触发改降级警告,只认 ``degraded:`` 前缀)。
        """
        evals = eval_set.evals
        if len(evals) <= 1:
            return eval_set
        groups: dict[str, list[EvalCase]] = {}
        order: list[str] = []
        for e in evals:
            key = (e.prompt or "").strip()
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(e)
        if len(groups) == len(evals):
            # 无同 prompt:不合并,仅按原序重排 id
            return EvalSet(skill_name=eval_set.skill_name,
                           evals=[e.model_copy(update={"id": i}) for i, e in enumerate(evals, 1)])

        merged: list[EvalCase] = []
        for new_id, key in enumerate(order, 1):
            group = groups[key]
            first = group[0]
            if len(group) == 1:
                merged.append(first.model_copy(update={"id": new_id}))
                continue
            seen_a, assertions = set(), []
            seen_x, expectations = set(), []
            seen_f, fixtures = {}, []
            seen_o, eouts = set(), []
            for e in group:
                for a in (e.assertions or []):
                    sig = (a.type, a.name, json.dumps(a.params or {}, sort_keys=True, ensure_ascii=False))
                    if sig not in seen_a:
                        seen_a.add(sig); assertions.append(a)
                for x in (e.expectations or []):
                    if x not in seen_x:
                        seen_x.add(x); expectations.append(x)
                for fx in (e.fixtures or []):
                    fsig = (fx.path or "", fx.content or "")
                    if fsig not in seen_f:
                        seen_f[fsig] = fx; fixtures.append(fx)
                eo = (e.expected_output or "").strip()
                if eo and eo not in seen_o:
                    seen_o.add(eo); eouts.append(eo)
            flags = list(dict.fromkeys([*(first.flags or []), f"merged:{len(group)}-evals"]))
            merged.append(EvalCase(
                id=new_id,
                name=first.name,
                prompt=first.prompt,
                expected_output="\n".join(f"- {x}" for x in eouts) if eouts else "",
                files=first.files,
                expectations=expectations,
                assertions=assertions,
                fixtures=fixtures,
                level="L2" if fixtures else first.level,
                mode=first.mode,
                source_dir=first.source_dir,
                source_dir_as=first.source_dir_as,
                timeout=first.timeout,
                flags=flags,
            ))
        return EvalSet(skill_name=eval_set.skill_name, evals=merged)

    def _extract_skill_outputs(self, body: str) -> set[str]:
        """确定性抽取 SKILL.md 声明的【产物】文件名 —— 不含输入/参考资料。

        mining 的客观断言受 ``_enforce_manifest`` closed-set 约束,manifest 默认只含
        transcript 真产出的文件;违规/不完整 transcript 没产出 skill 声明的产物时,连
        skill 明明要求的 ``FILE_EXISTS`` 也会被 drop(原则性断言本该 transcript 无关、
        源自 skill)。把 skill 声明的产物并入 manifest 即正本清源(方向 B)。

        两条高精信号,故不抓输入/参考资料里的文件名:
        (a) 输出语义小节(``## 输出`` / Output / 产物 / 交付物 …)内的所有文件名;
        (b) 步骤小节内、由写/产出动词(写出/生成/创建/保存 …)锚定的行上的文件名。
        """
        outputs: set[str] = set()
        for title, text in self._split_into_sections(body):
            names = {m.group(0) for m in _SKILL_FILENAME_RE.finditer(text)}
            if not names:
                continue
            low = title.lower()
            if _SKILL_OUTPUT_SECTION_RE.search(low):
                outputs |= names
            elif _SKILL_STEP_SECTION_RE.search(low):
                for line in text.split("\n"):
                    if _SKILL_WRITE_VERB_RE.search(line):
                        outputs |= {m.group(0) for m in _SKILL_FILENAME_RE.finditer(line)}
        return outputs

    def generate_from_transcript(self, skill_path: Path, transcript_path: Path,
                                 on_heartbeat: Callable[[str], None] | None = None) -> EvalSet:
        from skill_eval.evals.transcript import build_digest
        if not Path(transcript_path).exists():
            raise SkillEvalError(f"transcript 文件不存在: {transcript_path}")
        name, description, _content, _frontmatter, body = parse_skill_md(skill_path)
        digest = build_digest(transcript_path)
        if not digest.user_turns and not digest.manifest:
            raise SkillEvalError("transcript 未产出可用内容(无 user turn 也无文件)")
        skill_outputs = self._extract_skill_outputs(body)
        prompt = self._build_transcript_prompt(name, description, body, digest, skill_outputs)
        # mining 是单次大-prompt LLM 调用,常 1-5 分钟;"Mining…"后长时间静默像卡死。
        # 加心跳(独立线程,call_claude 阻塞期间每 20s 报一次仍在跑),同 audit/path-1 进度诉求。
        stop = threading.Event()
        if on_heartbeat:
            def _beat() -> None:
                secs = 0
                while not stop.wait(_HEARTBEAT_INTERVAL):
                    secs += _HEARTBEAT_INTERVAL
                    on_heartbeat(f"  …仍在调用 LLM 挖考卷({int(secs)}s)")
            threading.Thread(target=_beat, daemon=True).start()
        try:
            response = call_claude(prompt, model=self.model, timeout=self.timeout)
        except RuntimeError:
            stop.set()
            return self._transcript_fallback(name, description, digest)
        stop.set()
        eval_set = self._parse_llm_response(response, name)
        if eval_set is None:
            return self._transcript_fallback(name, description, digest)
        eval_set = self._enforce_manifest(eval_set, digest.manifest | skill_outputs)
        return self._merge_same_task_evals(eval_set)

    def _transcript_fallback(self, name: str, description: str, digest) -> EvalSet:
        turns = digest.user_turns[:8] or [self._build_fallback_prompt(name, description)]
        cases = [EvalCase(
            id=i, name=f"transcript-turn-{i}", level="L1", prompt=t[:500],
            expected_output="", expectations=["回应需贴合 transcript 里的真实任务"],
            assertions=[], flags=["degraded:no-llm"],
        ) for i, t in enumerate(turns, 1)]
        return EvalSet(skill_name=name, evals=cases)

    def _build_transcript_prompt(self, name: str, description: str, body: str, digest,
                                 skill_outputs: set[str] | None = None) -> str:
        skill_outputs = skill_outputs or set()
        manifest = sorted(digest.manifest | skill_outputs)
        produced = sorted(f.path for f in digest.produced_files)
        inputs = sorted(f.path for f in digest.input_files)
        L: list[str] = []
        L.append("You are an expert at creating evaluation test cases for AI coding assistants.\n")
        L.append("下面是 (a) Skill 定义 与 (b) 一份该 skill 真实使用 session 的 transcript digest。从 transcript 挖真实 evals,别凭空造。\n")
        L.append("## REAL USER REQUESTS\n")
        for i, t in enumerate(digest.user_turns, 1):
            L.append(f"[user {i}] {t[:1500]}\n")
        if digest.skill_used:
            L.append(f"\nSkill invoked: {digest.skill_used}\n")
        L.append("\n## INPUT FILES (作 fixture stage,真实内容)\n")
        for f in digest.input_files:
            L.append(f"--- {f.path} ---\n{f.content[:2000]}\n")
        L.append("\n## PRODUCED FILES (在其上断言 FILE_EXISTS / 内容)\n")
        for f in digest.produced_files:
            L.append(f"--- {f.path} ---\n{f.content[:2000]}\n")
        if digest.commands:
            L.append("\n## COMMANDS RUN + OUTPUTS (验证候选)\n")
            for c in digest.commands[:10]:
                L.append(f"$ {c.cmd[:300]}\n{c.stdout[:800]}\n")
        if digest.unreconstructed:
            L.append(f"\n## UNRECONSTRUCTED (Bash-only,别断言): {digest.unreconstructed}\n")
        L.append(f"\n## PATH MANIFEST (断言/fixture 路径只能引用这些): {manifest}\n")
        L.append(f"produced(FILE_EXISTS/内容目标): {produced}\ninput(fixtures): {inputs}\n")
        if skill_outputs:
            L.append(
                "\n## SKILL-DECLARED OUTPUTS (skill 要求的产物;此 transcript 未必产出,但遵循 "
                "skill 就该产 —— 可放心对它们出 FILE_EXISTS / 内容断言): "
                f"{sorted(skill_outputs)}\n"
            )
        L.append("\n## ASSERTION SCHEMA(scorer 读这些精确 key):\n"
                 '- FILE_EXISTS: {"filename": "<manifest path>"}\n'
                 '- CONTAINS_PATTERN / NOT_CONTAINS: {"pattern": "<regex>", "path": "<manifest path>"}(path 必填)\n'
                 '- JSON_HAS_FIELD: {"filename": "...", "field": "..."}\n'
                 '- CUSTOM_SCRIPT: {"script": "<checker .py>", "args": []}——checker 必须同时是该 eval 的 fixture\n')
        L.append("\n## trap-A WARNING: transcript 是一次执行,not ground truth。别把它的错/绕路冻成期望;推导一般可验证的期望;不确定就略。\n")
        L.append('\n## Output JSON ONLY(EvalSet schema): {"skill_name":"...","evals":'
                 '[{"id":1,"name":"...","level":"L1|L2","prompt":"...","expected_output":"...",'
                 '"expectations":["..."],"assertions":[{"name":"...","type":"...","params":{...}}],'
                 '"fixtures":[{"path":"...","content":"..."}]}]}\n')
        L.append("生成 5-8 条 eval(L1+L2 混)。语言同 transcript。\n")
        L.append(f"\n## SKILL\nname: {name}\ndescription: {description}\n\n{body[:6000]}\n")
        return "".join(L)
