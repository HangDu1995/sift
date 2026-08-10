"""Eval init generator — creates evals.json template for a Skill."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from skill_eval.errors import SkillEvalError
from skill_eval.models import Assertion, AssertionTier, AssertionType, EvalCase, EvalSet, TriggerProbe
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


# ---------------------------------------------------------------------------
# 同 prompt eval 合并前的一致性门(见 _merge_same_task_evals)
# ---------------------------------------------------------------------------
# 正则元字符:pattern 含任一即视为正则而非纯字面量(只有纯字面量才可安全做包含/常量比较)
_MERGE_REGEX_META = frozenset(".^$*+?{}[]\\|()")
# CONTAINS_PATTERN 字面量里的 "key": value 形状(JSON-ish)——用于抽同 key 不同常量
_MERGE_KV_RE = re.compile(r'"([\w\-]+)"\s*:\s*("[^"]*"|true|false|null|-?\d+(?:\.\d+)?)')


def _merge_is_literal_pattern(pattern: str) -> bool:
    """无正则元字符 → 纯字面量(可安全做包含/常量比较)。"""
    return bool(pattern) and not (_MERGE_REGEX_META & set(pattern))


def _merge_fixtures_conflict(group: list[EvalCase]) -> bool:
    """Gate 1:同 fixture path 出现 ≥2 不同 content → 不同 scenario,合并不安全。

    (原 union dedup key 本就是 ``(path, content)``,这里拿来做门:同 path 不同 content
    = transcript 成功段与失败段两套互斥 ground truth,合并必出自相矛盾题。)
    """
    by_path: dict[str, set[str]] = {}
    for e in group:
        for fx in (e.fixtures or []):
            by_path.setdefault(fx.path or "", set()).add(fx.content or "")
    return any(len(values) > 1 for values in by_path.values())


def _merge_assertions_conflict(group: list[EvalCase]) -> bool:
    """Gate 2:断言互相矛盾 → 合并必出自相矛盾题。保守:只判两种确定性矛盾。

    (a) CONTAINS_PATTERN 字面量 L 命中 NOT_CONTAINS 正则 R(同 file 作用域):一要含、一不许出现。
    (b) 同 (file,key) 的 CONTAINS_PATTERN 字面量钉成不同常量(按 ``"key": value`` 形状抽取)。

    跳过(证不了,保守放过):LLM_GRADE/LLM_FACT/CUSTOM_SCRIPT(语义)、正则对可满足性
    (不可判定)、含元字符的 CONTAINS(非字面量,证不了字面重叠)、主观 expectation 文本对立。
    """
    contains: list[Assertion] = []
    not_contains: list[Assertion] = []
    for e in group:
        for a in (e.assertions or []):
            if a.type == AssertionType.CONTAINS_PATTERN:
                contains.append(a)
            elif a.type == AssertionType.NOT_CONTAINS:
                not_contains.append(a)

    def _scope(a: Assertion) -> str:
        return (a.params.get("path") or "").strip()

    def _same_scope(p: str, q: str) -> bool:
        # 空 path = 未限定 = 覆盖任意显式 file;仅两者都显式且不同才视为不同作用域
        return (not p or not q) or p == q

    # (a) CONTAINS 字面量 ⊆ NOT_CONTAINS 正则
    for c in contains:
        cp = (c.params.get("pattern") or "")
        if not _merge_is_literal_pattern(cp):
            continue
        for n in not_contains:
            nr = (n.params.get("pattern") or "")
            if nr and _same_scope(_scope(c), _scope(n)):
                try:
                    if re.search(nr, cp):
                        return True
                except re.error:
                    continue

    # (b) 同 (file,key) 不同常量
    kv: dict[tuple[str, str], set[str]] = {}
    for c in contains:
        cp = (c.params.get("pattern") or "")
        if not _merge_is_literal_pattern(cp):
            continue
        scope = _scope(c)
        for m in _MERGE_KV_RE.finditer(cp):
            kv.setdefault((scope, m.group(1)), set()).add(m.group(2))
    if any(len(values) > 1 for values in kv.values()):
        return True

    return False


class EvalInitGenerator:
    def __init__(
        self,
        model: str | None = None,
        timeout: int = 120,
        config: EvalConfig | None = None,
        modes: list[str] | None = None,
        focus: str | None = None,
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.config = config
        self.modes = modes or []
        self._init_warnings: list[str] = []
        self._llm_usage: dict = {}
        self._llm_duration: float = 0.0
        self.focus = focus

    @property
    def init_warnings(self) -> list[str]:
        """LLM 生成时写了 EvalCase 不认的字段(拼错?)的警告;init 命令读去终端打印。

        getattr 容错:测试 mock ``__init__`` 时不设 ``_init_warnings`` 也安全可读(返 [])。
        """
        return list(getattr(self, "_init_warnings", []))

    @property
    def llm_usage(self) -> dict:
        """各 generate_* 的 call_claude 累计 token(on_usage 累加);init 终端显示用。"""
        return dict(getattr(self, "_llm_usage", {}))

    @property
    def llm_duration(self) -> float:
        """各 generate_* 的 call_claude 累计墙钟秒(含失败那次);init 终端显示用。"""
        return float(getattr(self, "_llm_duration", 0.0))

    def _llm_call(self, prompt: str) -> str:
        """call_claude + 累加 token/耗时到实例(init 终端显示用)。RuntimeError 透传调用方处理。"""
        usage: dict = {}
        start = time.perf_counter()
        try:
            return call_claude(prompt, model=self.model, timeout=self.timeout, on_usage=usage.update)
        finally:
            self._llm_duration += time.perf_counter() - start
            # 真实 claude json 的 usage 含非数字字段(server_tool_use dict / service_tier
            # str 等),只累加数字 value,否则 int+dict 会 TypeError(测试 mock 用简化
            # payload 抓不到,须实跑或模拟真实结构)。
            for k, v in usage.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
                    self._llm_usage[k] = self._llm_usage.get(k, 0) + v

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

    def generate_with_llm(self, skill_path: Path,
                          existing_evals: list[EvalCase] | None = None) -> EvalSet:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)
        is_procedural = self._is_procedural_skill(content, skill_path)

        prompt = self._build_llm_prompt(name, description, content, is_procedural,
                                        focus=self.focus, existing_evals=existing_evals)
        try:
            response = self._llm_call(prompt)
        except RuntimeError:
            return self.generate_template(skill_path)

        eval_set = self._parse_llm_response(response, name)
        if eval_set is not None:
            self._flag_missing_rationale(eval_set)
            for case in eval_set.evals:
                if case.level == "L1":
                    case.assertions = self._auto_generate_assertions(case)
            return eval_set

        return self.generate_template(skill_path)

    # ------------------------------------------------------------------
    # Agent 出题(D6):从 description/角色推 role-matching 任务。
    # 与 skill generate_with_llm 不同:不按正文切段(skill 工作流语义)、不假设产文件;
    # 断言靠 expectations 自动派生。共享 _llm_call/_parse_llm_response/_auto_generate_assertions。
    # ------------------------------------------------------------------

    def _build_agent_prompt(self, name: str, description: str, body: str,
                            frontmatter: dict, focus: str | None = None,
                            existing_evals: list | None = None) -> str:
        tools = frontmatter.get("tools", [])
        existing_note = ""
        if existing_evals:
            prompts = "\n".join(f"- {e.prompt[:60]}" for e in existing_evals[:8])
            existing_note = f"\n## 已有题(别重复,出新角度)\n{prompts}\n"
        focus_note = f"\n## 定向补题方向\n{focus}\n" if focus else ""
        return f"""你在为一个 AI agent 设计评测用例。被评的是 agent 定义本身(system prompt 角色),不是 skill。

## Agent
name: {name}
description: {description}

## Agent system prompt(角色指令正文)
{body}

## Agent 声明的工具白名单
{tools}{existing_note}{focus_note}

## 任务
生成 5-8 道真实、会交给这个 agent 的考题。每道题要能区分好/坏 response(太 trivial 区分不了就没评测价值),且必须匹配 description 声明的角色(它说管 X 就出 X 的题,不出它不该管的)。

## Output JSON (ONLY this, no other text)
```json
{{
  "skill_name": "{name}",
  "evals": [
    {{
      "id": 1,
      "name": "agent-<scenario>",
      "level": "L1",
      "prompt": "主 agent / 用户会把这件事怎么交给它(具体场景 + 必要上下文,别泛泛)",
      "expected_output": "好的完成长什么样(可观测的质量标准)",
      "expectations": [
        "单条、具体、可观测的预期。GOOD: 'response 指出具体行号'。BAD: '输出正确' / '遵循指令'(空泛不可查)"
      ],
      "assertions": [
        {{ "name": "mentions X", "type": "CONTAINS_PATTERN", "params": {{ "pattern": "keyword", "scope": "transcript" }} }},
        {{ "name": "avoids Y", "type": "NOT_CONTAINS", "params": {{ "pattern": "forbidden", "scope": "transcript" }} }}
      ],
      "fixtures": [
        {{ "path": "src/auth.py", "content": "真实文件内容(带要审的问题 / 要跑的代码),agent 会真去 Read/Grep 它" }}
      ],
      "rationale": "这题考 agent 哪条职责(对应 description / 正文哪点;同 agent 声明语言)"
    }}
  ]
}}
```

## Rules
- 语言:同 agent 声明正文
- expectations 一律写**完整句子**(有动词),包括格式/总评类(行号、严重度、总评结论)——别压成名词堆标签。BAD:"总评说明阻塞项与能否合并" / "带 文件:行号 + 严重度";GOOD:"总评里说清有哪些阻塞项、这个 PR 能不能合" / "每条建议都标了 文件:行号 和严重度"
- level: L1 = 不产文件 / 纯分析建议(大多数 agent);L2 = 产文件(产出的文件名写进 assertions 的 FILE_EXISTS)
- 每条 expectation 都写对应 assertion(s)(可一条拆多个、可加隐含的);L1 用 CONTAINS_PATTERN / NOT_CONTAINS 且 **必须带 "scope":"transcript"**——只读 agent 不产文件,断言查 agent 的回答文本(transcript),不查输入文件(带 path 或省 scope 会去读输入 fixture 而非 agent 的回答,判卷失效);L2 产文件才用 "path":"<产物文件>" + FILE_EXISTS
- fixtures:若任务涉及文件(审代码 / 跑测试 / 读配置——agent 会用 Read/Grep/Bash 碰文件),把相关文件作为 fixtures 落地(prompt 用**路径**引用,如「审查 src/auth.py 的改动」,**别把代码塞进 prompt 文本**——agent 要能真 Read/Grep 文件才算测到它的工作流,塞 prompt 里它就没文件可碰);纯建议、无文件操作的 agent 不带 fixtures。fixture 是**输入**(给 agent 读 / 跑的),断言仍用 scope:"transcript" 查 agent 的回答,**不要**用 path 指 fixture
- rationale 每题必填:说清考哪条职责
- 总 5-8 道,至少 1 道边界 / 错误场景"""

    def generate_for_agent(self, agent_path: Path,
                           existing_evals: list | None = None) -> EvalSet:
        """agent 出题:从 description/角色推 role-matching 任务 + 质量线。

        跟 skill generate_with_llm 不同:不按正文切段(skill 工作流语义)、不假设产文件。
        断言由 LLM 直接写(对齐 skill _build_llm_prompt);_auto_generate_assertions 作兜底
        (LLM 写够的 assertions 直接用,自然语言 expectations 派生抽不到)。LLM 失败 / 解析失败
        → 空 EvalSet(agent 无确定性 fallback;generate_template 的切段是 skill 专属,不适用)。
        """
        name, description, content, frontmatter, body = parse_skill_md(agent_path)
        prompt = self._build_agent_prompt(name, description, body, frontmatter,
                                          focus=self.focus, existing_evals=existing_evals)
        try:
            response = self._llm_call(prompt)
        except RuntimeError:
            return EvalSet(skill_name=name, evals=[])
        eval_set = self._parse_llm_response(response, name)
        if eval_set is None:
            return EvalSet(skill_name=name, evals=[])
        self._flag_missing_rationale(eval_set)
        for case in eval_set.evals:                       # agent 两级都派生(L2 也补 FILE_EXISTS)
            case.assertions = self._auto_generate_assertions(case)
        for case in eval_set.evals:                       # 断言偏少 → init 提示审(兜底硬约束下漏写的少数)
            if len(case.assertions) < len(case.expectations):
                self._init_warnings.append(
                    f"题{case.id} {case.name}: 断言 {len(case.assertions)} < 期望 "
                    f"{len(case.expectations)},扫一眼该补的可程序化项"
                    f"(纯主观 expectation 无断言正常)")
        return eval_set

    # ------------------------------------------------------------------
    # Trigger-accuracy probes — 读 description 生成 should/should-not 探题
    # (Task 6;init --trigger-probes 在 Task 7 接线)
    # ------------------------------------------------------------------

    def generate_trigger_probes(self, skill_path: Path, num_each: int = 5) -> list[TriggerProbe]:
        """LLM 读 description 生成 should-trigger + should-not-trigger 探题。

        探题测的是 cc 会不会**光看 description** 就自然触达本 skill,故 prompt 强制 neutral
        wording、不命名 skill(在 prompt 里点名就只是测字面匹配,不是测触发)。should-not 取
        plausible-but-wrong(看着像但本 skill 真不该管 —— 测 false-trigger / 过宽 description)。
        LLM 失败 → 返 [](under-delivery / trim 留 init 层处理,不在此强造)。
        """
        name, description, *_ = parse_skill_md(skill_path)
        prompt = self._build_trigger_prompt(name, description, num_each)
        try:
            response = self._llm_call(prompt)
        except RuntimeError:
            return []
        return self._parse_trigger_response(response)

    @staticmethod
    def _build_trigger_prompt(name: str, description: str, num_each: int) -> str:
        return f"""You are generating trigger-accuracy probes for a Claude Code Skill.

Skill name: {name}
Skill description: {description}

Generate {num_each} should-trigger prompts + {num_each} should-not-trigger prompts.
- should-trigger (expect_trigger=true): a REAL task this skill should handle, written as a
  user request. CRITICAL: use neutral wording — DO NOT name or hint at the skill in the prompt
  (the test is whether the model reaches for the skill from its description alone). Paraphrase
  the description's intent — do NOT copy phrases from it verbatim (word-for-word overlap would
  test literal matching, not whether the model understands the intent).
- should-not-trigger (expect_trigger=false): a task that looks plausibly related but where
  this skill should NOT fire (tests false-trigger / over-broad description).

Each probe needs a one-line rationale (why it should/shouldn't trigger).

Output JSON ONLY:
{{"skill_name": "{name}", "trigger_probes": [
  {{"id": 1, "prompt": "...", "expect_trigger": true, "rationale": "..."}},
  ...
]}}

## Output JSON
"""

    def _parse_trigger_response(self, response: str) -> list[TriggerProbe]:
        """两段式解析(同 ``_parse_llm_response``):直接 ``json.loads`` 失败 → 正则抽 ``{...}``。

        每条 probe 单独 try(某条畸形不影响其余);整体解析不出 → [](不造探题)。
        """
        try:
            data = json.loads(response)
        except (json.JSONDecodeError, Exception):
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                return []
            try:
                data = json.loads(match.group())
            except (json.JSONDecodeError, Exception):
                return []
        probes: list[TriggerProbe] = []
        for i, item in enumerate(data.get("trigger_probes", []) or [], 1):
            try:
                probes.append(TriggerProbe(
                    id=item.get("id", i),
                    prompt=item.get("prompt", ""),
                    expect_trigger=bool(item.get("expect_trigger")),
                    rationale=item.get("rationale", ""),
                ))
            except Exception:
                continue
        return probes

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

    # --focus 预设类:补 LLM 一次性生成整套时的通用盲区(预设兜无意识盲区,自由文本兜 skill 特定方向)
    _FOCUS_PRESETS = {
        "negative-cases": (
            "## Focus: Negative scenarios\n\n"
            "ONLY generate evals for scenarios where this skill should **NOT** be triggered, "
            "or should **refuse / hand off / warn** — wrong input type, out-of-scope request, "
            "ambiguous trigger, precondition not met. Default generation over-indexes on "
            "happy-path / positive triggers; this run deliberately fills the negative-coverage gap.\n\n"
        ),
        "edge-cases": (
            "## Focus: Edge cases & error handling\n\n"
            "ONLY generate evals for boundary / error conditions: empty input, oversized input, "
            "malformed / corrupted input, missing fields, abnormal values, failure recovery. "
            "Default generation over-indexes on happy-path; this run deliberately fills the "
            "edge / error-coverage gap.\n\n"
        ),
    }
    # 旧名 anti-triggers 已在公开 main 上过,保留静默 back-compat 别名(否则未知 key 会走
    # 自由文本回退,把 "anti-triggers" 当字面方向 → 静默错行为)。新文档/测试一律用 negative-cases。
    _FOCUS_ALIASES = {"anti-triggers": "negative-cases"}

    @staticmethod
    def _focus_directive(focus: str | None) -> str:
        """--focus → 注入 prompt 的定向指令段(None/空 → "",默认生成整套不变)。

        预设类(negative-cases / edge-cases)补 LLM 生成整套时的**通用盲区**(prompt 精调,
        强制反面 / 边界思维);其余当**自由文本**(补 skill 特定薄弱面,如"加密 PDF")。
        通常配 ``--append`` 增量补到已有 evals.json(否则覆盖式只换套题,价值低)。
        """
        if not focus or not focus.strip():
            return ""
        key = focus.strip().lower()
        key = EvalInitGenerator._FOCUS_ALIASES.get(key, key)  # 旧名 back-compat
        if key in EvalInitGenerator._FOCUS_PRESETS:
            return EvalInitGenerator._FOCUS_PRESETS[key]
        return (
            "## Focus: Targeted supplement (user-specified)\n\n"
            "ONLY generate evals for this specific direction:\n"
            f"> {focus.strip()}\n\n"
            "Default generation may have under-covered this; this run targets it deliberately.\n\n"
        )

    def _supplement_directive(self, focus: str | None, existing_evals) -> str:
        """定向补题指令:有现有题(init --append / coverage --augment)→ 硬化;无 → 退回软 _focus_directive。

        治 init --append --focus 半聚焦 + 不去重:旧版只把 focus 作软 directive 注到「生成整套」
        prompt 前,LLM 仍端到端生成一遍、且与现有题重复。有现有题时改用纯补题指令——明确
        「只补 focus 这一面、且现有未覆盖的」,并把现有题清单(name + prompt + rationale)喂进去
        让 LLM 语义避重(对照 prompt **和** rationale,不只字面)。init / coverage --augment 共用
        此路径(_build_llm_prompt 一处改,两路达标)。
        """
        if not existing_evals:
            # 无现有题(覆盖式 init)→ 行为不变,走软 focus directive
            return self._focus_directive(focus)
        lines = []
        for e in existing_evals[:30]:
            rat = (e.rationale or "").strip().replace("\n", " ")
            prm = (e.prompt or "").strip().replace("\n", " ")
            lines.append(f'- [{e.id}] "{e.name}" — prompt: {prm[:90]} | rationale: {rat[:110]}')
        existing_block = "\n".join(lines)
        focus_line = (f"**Targeted gap to fill THIS run:** {focus.strip()}\n\n"
                      if (focus and focus.strip()) else "")
        return (
            "## THIS IS A TARGETED SUPPLEMENT, NOT a fresh full suite\n\n"
            "evals.json already has the existing evals listed below. DO NOT regenerate or "
            "duplicate them. Generate ONLY new evals that:\n"
            "1. directly test the targeted gap named below, AND\n"
            "2. are NOT already covered by any existing eval — compare BOTH the prompt AND the "
            "rationale of each existing eval, because different wording can still test the same "
            "rule (e.g. an existing 'empty log → total_errors=0' eval already covers that edge).\n"
            "Every assertion MUST verify the targeted gap. If a candidate eval is really about a "
            "different rule (categorization, output format, fabrication guard, …) that the existing "
            "evals already cover, DROP it — do not pad the suite with off-topic cases.\n"
            "Keep it tight: 2-5 high-quality gap-filling evals, not a fresh full set.\n\n"
            f"{focus_line}"
            f"**Existing evals already cover (DO NOT duplicate):**\n{existing_block}\n\n"
        )

    def _build_llm_prompt(self, name: str, description: str, content: str,
                          is_procedural: bool = False, focus: str | None = None,
                          existing_evals: list[EvalCase] | None = None) -> str:
        if is_procedural:
            prompt = self._build_procedural_prompt(name, description, content)
        else:
            prompt = self._build_knowledge_prompt(name, description, content)
        directive = self._supplement_directive(focus, existing_evals)
        if directive:
            # 注在 ## Output JSON 前(两个 builder 都有该锚点),改变这次生成目标
            prompt = prompt.replace("## Output JSON", directive + "## Output JSON", 1)
        return prompt

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
            '      "fixtures": [],\n'
            '      "rationale": "<which SKILL.md section/bullet/quote this eval tests — SAME language as SKILL.md>",\n'
            '      "confidence": 0.0-1.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Rules\n"
            "- Every eval MUST include `rationale`: cite the exact SKILL.md span it tests (section / bullet / quoted rule, same language as SKILL.md). `confidence` (0.0-1.0) = how sure you are this eval cleanly tests that rule (for human review triage).\n"
            "- 5-10 evals covering the most important rules\n"
            "- NO vague expectations — every one must be objectively checkable\n"
            "- Every expectation is a FULL SENTENCE with a verb — including format/structure requirements (output shape, ordering, fields). "
            "Never compress to a noun-only label. BAD: 'correct output format'; "
            "GOOD: 'the response lists each entry with its file name and line number'.\n"
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
            '      "fixtures": [],\n'
            '      "rationale": "<SKILL.md span this eval tests — same language as SKILL.md>",\n'
            '      "confidence": 0.0-1.0\n'
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
            "      ],\n"
            '      "rationale": "<SKILL.md span this eval tests — same language as SKILL.md>",\n'
            '      "confidence": 0.0-1.0\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "## Rules\n"
            "- Every eval MUST include `rationale`: cite the exact SKILL.md span it tests (section / bullet / quoted rule, same language as SKILL.md). `confidence` (0.0-1.0) = how sure you are this eval cleanly tests that rule (for human review triage).\n"
            "- Total 8-12 evals\n"
            "- L1: test rules/decisions (no fixtures). L2: test actual execution (with fixtures).\n"
            "- L2 fixtures must be REALISTIC minimal test data that Claude can actually process.\n"
            "- For text-based fixtures (JSON/code), include the ACTUAL content. "
            "For binary fixtures (PDF/image), write a minimal placeholder.\n"
            "- L2 expectations must mention CONCRETE output: file names, counts, patterns.\n"
            "- Every expectation is a FULL SENTENCE with a verb — including format/structure requirements. "
            "Never a noun-only label. BAD: 'correct output format'; "
            "GOOD: 'the generated file lists each entry with its name and line number'.\n"
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
                self._init_warnings.extend(self._collect_unknown_field_warnings(data))
                return EvalSet.model_validate(data)
        except (json.JSONDecodeError, Exception):
            pass

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                if "evals" in data:
                    self._init_warnings.extend(self._collect_unknown_field_warnings(data))
                    return EvalSet.model_validate(data)
            except Exception:
                pass

        return None

    @staticmethod
    def _flag_missing_rationale(eval_set: EvalSet) -> None:
        """rationale 空 → 标 review:no-rationale(不拒;拒会掉确定性 fallback 更差)。

        rationale 是审阅抓手 + 路 B instruction_ref 种子,缺了要让人看见、人工补。
        (Waza validateTaskMetadata 无 rationale 直接拒;我们 flag 不拒——拒绝会把
        整批 LLM eval 作废掉进 generate_template 确定性 fallback,反而更差。)
        """
        for ev in eval_set.evals:
            if not (ev.rationale or "").strip():
                ev.flags = list(dict.fromkeys([*(ev.flags or []), "review:no-rationale"]))

    @staticmethod
    def _collect_unknown_field_warnings(data: dict) -> list[str]:
        """raw LLM 输出含 EvalCase 不认的字段 → 收集警告字符串列表。

        EvalCase 走 pydantic 默认 extra='ignore',未知字段(如把 ``expected_output``
        拼成 ``expected``、``assertions`` 拼成 ``assertion``)会被**静默丢弃** ——
        init 看着生成成功、考卷实则缺字段,run 时才暴雷。本检查在 ``model_validate``
        之前扫 raw dict,把可疑字段亮出来供 init 审阅(warn 不拒;拒会掉确定性
        fallback 更差,同 ``_flag_missing_rationale`` 哲理)。已知字段集取自
        ``EvalCase.model_fields``,自动随 model 演化,无需手维护。
        """
        known = set(EvalCase.model_fields.keys())
        warns: list[str] = []
        raw_evals = data.get("evals") if isinstance(data, dict) else None
        if not isinstance(raw_evals, list):
            return warns
        for i, raw in enumerate(raw_evals):
            if not isinstance(raw, dict):
                continue
            unknown = sorted(set(raw.keys()) - known)
            if unknown:
                ident = raw.get("id", f"第{i + 1}条")
                warns.append(f"eval {ident}: 未知字段 {unknown}(已忽略;可能拼错)")
        return warns

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

        合并前过两道确定性一致性门,任一冲突 → 不合并(各 eval 原样独立保留、仅重排 id,
        flag ``split:N-evals-conflict``):(1) fixture 冲突:同 path 出现 ≥2 不同 content
        (不同 scenario,如成功/失败两段 transcript);(2) 断言冲突:CONTAINS 字面量命中某
        NOT_CONTAINS 正则,或同 (file,key) 钉成不同常量。模糊/语义断言不参与(证不了,
        保守放过)——只合并能确定一致的组(见 ``_merge_fixtures_conflict`` / ``_merge_assertions_conflict``)。
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
        next_id = 1
        for key in order:
            group = groups[key]
            first = group[0]
            if len(group) == 1:
                merged.append(first.model_copy(update={"id": next_id}))
                next_id += 1
                continue
            if _merge_fixtures_conflict(group) or _merge_assertions_conflict(group):
                # 确定性冲突 → 不合并:各 eval 原样独立保留(仅重排 id),flag 留痕
                split_flag = f"split:{len(group)}-evals-conflict"
                for e in group:
                    flags = list(dict.fromkeys([*(e.flags or []), split_flag]))
                    merged.append(e.model_copy(update={"id": next_id, "flags": flags}))
                    next_id += 1
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
                id=next_id,
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
                rationale=first.rationale,
                confidence=first.confidence,
            ))
            next_id += 1
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
                                 on_heartbeat: Callable[[str], None] | None = None,
                                 kind: str | None = None) -> EvalSet:
        from skill_eval.evals.transcript import build_digest
        if not Path(transcript_path).exists():
            raise SkillEvalError(f"transcript 文件不存在: {transcript_path}")
        name, description, _content, frontmatter, body = parse_skill_md(skill_path)
        digest_path = transcript_path
        if kind:
            # 多单元 transcript:只取本 skill/agent 被调用/被 dispatch 期间的段(kind 选锚点),
            # 拼成虚拟 transcript 再 build_digest → manifest 只含本单元文件,不串别单元的味。
            from skill_eval.audit.slice import slice_transcript
            segments = slice_transcript(transcript_path, name, kind)
            if not segments:
                raise SkillEvalError(
                    f"「{name}」未在此 transcript 以 --kind {kind} 锚点出现,无可挖段")
            import tempfile
            flat = [r for seg in segments for r in seg]
            tf = tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
            tf.write("\n".join(json.dumps(r, ensure_ascii=False) for r in flat) + "\n")
            tf.close()
            digest_path = Path(tf.name)
        digest = build_digest(digest_path)
        if not digest.user_turns and not digest.manifest:
            raise SkillEvalError("transcript 未产出可用内容(无 user turn 也无文件)")
        skill_outputs = self._extract_skill_outputs(body)
        prompt = self._build_transcript_prompt(name, description, body, digest, skill_outputs,
                                               kind=kind, frontmatter=frontmatter)
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
            response = self._llm_call(prompt)
        except RuntimeError:
            stop.set()
            return self._transcript_fallback(name, description, digest)
        stop.set()
        eval_set = self._parse_llm_response(response, name)
        if eval_set is None:
            return self._transcript_fallback(name, description, digest)
        self._flag_missing_rationale(eval_set)
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
                                 skill_outputs: set[str] | None = None,
                                 kind: str | None = None,
                                 frontmatter: dict | None = None) -> str:
        skill_outputs = skill_outputs or set()
        is_agent = kind == "agent"
        manifest = sorted(digest.manifest | skill_outputs)
        produced = sorted(f.path for f in digest.produced_files)
        inputs = sorted(f.path for f in digest.input_files)
        L: list[str] = []
        if is_agent:
            L.append("You are an expert at creating evaluation test cases for AI agents.\n")
            L.append("下面是 (a) Agent system prompt(角色指令正文) 与 (b) 一份该 agent 真实使用 session 的 transcript digest。"
                     "从 transcript 挖真实 evals(会交给这个 agent 的任务),别凭空造。\n")
            L.append("## Agent 声明的工具白名单\n" + str((frontmatter or {}).get('tools', [])) + "\n")
        else:
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
                 '- CONTAINS_PATTERN / NOT_CONTAINS: {"pattern": "<regex>", "path": "<manifest path>"}(查产物文件内容)'
                 ' 或 {"pattern": "<regex>", "scope": "transcript"}(只查 agent/skill 文本输出,不碰文件——只读/纯建议 agent 用这个,别 path 指输入文件)\n'
                 '- JSON_HAS_FIELD: {"filename": "...", "field": "..."}\n'
                 '- CUSTOM_SCRIPT: {"script": "<checker .py>", "args": []}——checker 必须同时是该 eval 的 fixture\n')
        L.append("\n## trap-A WARNING: transcript 是一次执行,not ground truth。别把它的错/绕路冻成期望;推导一般可验证的期望;不确定就略。\n")
        rationale_hint = ("对应 agent 正文哪条职责 或 transcript 哪个真实行为" if is_agent
                          else "对应 SKILL.md 哪段/哪条 或 transcript 哪个行为")
        L.append('\n## Output JSON ONLY(EvalSet schema): {"skill_name":"...","evals":'
                 '[{"id":1,"name":"...","level":"L1|L2","prompt":"...","expected_output":"...",'
                 '"expectations":["..."],"assertions":[{"name":"...","type":"...","params":{...}}],'
                 '"fixtures":[{"path":"...","content":"..."}],'
                 '"rationale":"<' + rationale_hint + '>","confidence":0.0-1.0}]}\n')
        if is_agent:
            L.append("生成 5-8 条 eval。语言同 transcript。匹配 description 声明的角色(它说管 X 就出 X 的题,不出它不该管的)。\n")
            L.append("level: L1=不产文件/纯分析建议(大多数 agent,如审 PR 给建议);产口头建议时挖 review 文本里的"
                     "行号/严重度/关键词 → CONTAINS_PATTERN/NOT_CONTAINS **带 scope:'transcript'**(查 agent 回答文本,"
                     "别用 path 指输入文件——否则 grading 读输入 fixture 而非 agent 的回答,判卷失效);"
                     "L2=产文件才 FILE_EXISTS + path 指产物。\n")
        else:
            L.append("生成 5-8 条 eval(L1+L2 混)。语言同 transcript。\n")
        L.append(f"每条 eval 必须带 rationale({rationale_hint})与 confidence(0-1,审阅分诊用)。\n")
        L.append("expectations 一律写完整句子(有动词),包括格式/总评类(行号、严重度、总评结论)——别压成名词堆标签。"
                 "BAD:\"总评说明阻塞项与能否合并\";GOOD:\"总评里说清有哪些阻塞项、这个 PR 能不能合\"。\n")
        seg_head = "## Agent system prompt" if is_agent else "## SKILL"
        L.append(f"\n{seg_head}\nname: {name}\ndescription: {description}\n\n{body[:6000]}\n")
        return "".join(L)
