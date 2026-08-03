"""Instruction extractor — extracts checkable instructions from SKILL.md body."""

from __future__ import annotations

import re

from skill_eval.i18n import ANTI_FABRICATION_CN  # 反捏造词与 static_analysis/checks.py 共用(P2-13)
from skill_eval.models import (
    CheckMethod,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
)

# 中文关键词 — extractor 独用(单模块词表留原处);反捏造词已移至 skill_eval.i18n 共享(P2-13)。
_CONSTRAINT_KEYWORDS_CN: tuple[str, ...] = ("禁止", "严禁", "不得", "不应", "不可", "必须")
# 跑/使用 是 agent 定义里引出脚本/工具的高频动词(st-verifier「跑 coverage_audit.py」、
# developer「使用 Bash/Write/Edit 工具」),原表缺 → 这些 TOOL_REQUIREMENT 全漏。
_TOOL_VERBS_CN: tuple[str, ...] = ("运行", "执行", "调用", "启动", "跑", "使用")
# TOOL_REQUIREMENT 停用词:"动词(use/run/使用/…)+ 这些词"构成的 match 不是真工具名,滤掉。
# 分两类(见各组行内注释):(a) 虚词/碎片——"use when"/"run concurrently" 被误抽(audit 实测
# tool_name="when",LLM 判"该用工具 when 但没用"→ 假 FAIL;跨 104 skill 验证 0 假阴性);
# (b) 格式/协议/标准名——「使用 JSON」「使用 API」(agent/skill 定义高频,cannbot-skills
# 实测活体命中)被误抽,下游 match_tool_call 条件3 `\bJSON\b` 非确定性命中(读过 config.json
# → 假 PASS;没读 → 假 FAIL)→ verdict 噪声。裸用一律停用;含连字符复合名(csv-parser)或
# CLI 名(psql)整体抽出、不受影响(见 test_tool_requirement_stopwords_*)。
_TOOL_STOPWORDS: frozenset[str] = frozenset({
    # (a) 虚词/碎片:"动词 + 这些词"不是真工具名。
    "when", "and", "the", "a", "an", "full", "all", "this", "that", "it", "they",
    "you", "your", "their", "concurrently", "simultaneously", "together", "in",
    "on", "at", "to", "of", "for", "with", "by", "from", "both", "each", "some",
    "any", "more",
    # (b) 格式/协议/标准名:裸用(「使用 JSON」「使用 API」)被误抽成工具名,但它们不是工具。
    "json", "yaml", "xml", "html", "css", "csv", "tsv",
    "api", "http", "https", "rest",
    "sql",
    "url", "uri",
})


_PARAM_STOPPERS: frozenset[str] = frozenset({
    # 连接词:其后通常是下一个工具/另一动作,不是当前工具的参数 → 遇到即停(一行多工具保护)。
    "和", "或", "还有", "以及", "然后", "并", "接着", "同时",
    "and", "or", "then", "also", "plus", "+", "&",
})


def _extract_required_params(rest: str) -> list[str]:
    """从 tool_name 后的剩余文本抽参数 token(粗召回,供 match_tool_call 参数级断言)。

    只留 ASCII token(CLI flag/positional/subcommand,如 ``--tb=short``/``-x``/``tests/``),
    滤中文/非 ASCII 描述、虚词(复用 ``_TOOL_STOPWORDS``)、纯标点。**遇连接词(和/and/or/…)即停**
    ——一行多工具时(``使用 Grep 和 shape_audit``)保护:不让后面的工具名被当成前一个工具的参数。
    粗召回:抽多了/错了在 match 断言不上就当没有(走 LLM 兜底),天然容错。
    """
    if not rest:
        return []
    tokens: list[str] = []
    for raw in rest.split():
        tok = raw.strip(".,;:!'\"`*()[]{}")
        if not tok:
            continue
        if tok.lower() in _PARAM_STOPPERS:
            break  # 连接词:参数序列结束(后面是别的工具/动作)
        if not tok.isascii():
            continue  # 中文描述等跳过(参数是 ASCII CLI token)
        if tok.lower() in _TOOL_STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


class InstructionExtractor:
    def extract(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        sections = self._split_sections(body)

        for section_name, section_text in sections:
            checks.extend(self._extract_heading_steps(section_name, section_text))
            checks.extend(self._extract_tool_requirements(section_name, section_text))

        checks.extend(self._extract_constraints(body))
        checks.extend(self._extract_anti_fabrication(body))
        checks.extend(self._extract_output_format(body))
        checks.extend(self._extract_reference_usage(body))

        return checks

    def _split_sections(self, body: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_heading = "Root"
        current_lines: list[str] = []

        for line in body.split("\n"):
            if line.startswith("#"):
                if current_lines:
                    sections.append((current_heading, "\n".join(current_lines)))
                current_heading = line.lstrip("#").strip()
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            sections.append((current_heading, "\n".join(current_lines)))

        return sections

    def _extract_heading_steps(self, section: str, text: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        lines = text.strip().split("\n")
        imperative_re = re.compile(r"^[*\-]\s+\**[A-Z]")

        for line in lines:
            stripped = line.strip()
            m = re.match(r"^\d+\.\s+", stripped)
            if not m and not stripped.startswith(("* ", "- ")):
                continue
            clean = re.sub(r"^[*\-]\s+|\d+\.\s+", "", stripped).strip()
            clean = re.sub(r"\*\*", "", clean)
            if not clean or len(clean) < 5:
                continue

            checks.append(InstructionCheck(
                source=InstructionSource.HEADING_STEP,
                instruction_text=clean,
                section=section,
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.LLM_JUDGE,
            ))

        return checks

    def _extract_tool_requirements(self, section: str, text: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        # 英文:动词 + 必有空格 + 可选 "the" + 工具名。\s+ 防 "using" 被拆成 use+ing。
        # 中文:动词(常无空格)+ 英文工具名(中文工具名无法对应 trace 里的工具调用,不提取)。
        # 工具名必须 ASCII 开头([A-Za-z],可含 - 和 .,如 npu-smi / check_env.sh):
        # 旧 \w+ 在 Unicode 下匹配汉字,把「运行时依赖基线」抽成 tool_name='时依赖基线'
        # —— ascendc-env-check 实测 6 条 TOOL_REQUIREMENT 里 5 条是「运行时/运行中/运行失败」
        # 复合词假阳性(运行=runtime 非动词 run)。对齐上面注释的设计本意。
        patterns = [
            re.compile(r"(?:run|execute|use|call|invoke)\s+(?:the\s+)?([A-Za-z][\w\-\.]*)", re.IGNORECASE),
            # 动词后允许任意反引号/星号序列 + 可选 ``./`` 再取工具名:markdown 习惯把工具名/脚本名
            # 用反引号或粗体包(`` `Grep` `` / `` `parse_result.py` `` / `` **`shape_audit.py`** ``),
            # 脚本常带 ``./`` 前缀(`` `./build.sh` ``)。原 ``\s*`` 后直接要求 ASCII 字母,这些包裹+点
            # 挡住 → 全漏。tool_name 取名字部分(不含 ``./``),match_tool_call 第 3 条按词边界命中
            # input_summary 里的 "./build.sh"。`` [`*]* `` 只吃反引号/星号(纯 markdown 包裹,不入工具名)。
            re.compile(rf"(?:{'|'.join(_TOOL_VERBS_CN)})\s*[`*]*(?:\./)?([A-Za-z][\w\-\.]*)"),
        ]
        for pattern in patterns:
            for match in pattern.finditer(text):
                tool_name = match.group(1)
                # 停用词过滤:"use when"/"run concurrently" 等碎片不是真工具(见 _TOOL_STOPWORDS)。
                if tool_name.lower() in _TOOL_STOPWORDS:
                    continue
                # rest = tool_name 后到行尾(pattern 到 tool_name 结束,match.end(0)=tool_name 末),
                # 抽 required_params(遇连接词停,保护一行多工具;见 _extract_required_params)。
                rest = text[match.end(0):].split("\n", 1)[0]
                required_params = _extract_required_params(rest)
                checks.append(InstructionCheck(
                    source=InstructionSource.TOOL_REQUIREMENT,
                    instruction_text=match.group(0).strip(),
                    section=section,
                    priority=InstructionPriority.REQUIRED,
                    check_method=CheckMethod.TOOL_CALL_CHECK,
                    params={"tool_name": tool_name, "required_params": required_params},
                ))
        return checks

    def _extract_constraints(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        # 中文约束词后跟至少 2 字内容(中文信息密度高,2 字 ≈ 英文 5 字);`.` 默认不跨行。
        # "不可" 后跟这些字构成普通词(不可用/不可见/不可数/不可能...),非禁止性约束 →
        # 负向前瞻排除,否则把「不可用时的回退方案见 X」误当约束(ascendc-env-check 实测)。
        # "不可遗漏/不可省略" 等真约束保留(后跟字不在排除表)。
        cn = "|".join(
            rf"不可(?!用|见|数|能|逆|抗|解|测|及)" if k == "不可" else k
            for k in _CONSTRAINT_KEYWORDS_CN
        )
        pattern = re.compile(
            r"(NEVER|ALWAYS|MUST\s+NOT|DO\s+NOT|SHALL\s+NOT|SHOULD\s+NOT)\s+.{5,}"
            rf"|(?:{cn}).{{2,}}",
            re.IGNORECASE,
        )
        for match in pattern.finditer(body):
            checks.append(InstructionCheck(
                source=InstructionSource.CONSTRAINT,
                instruction_text=match.group(0).strip(),
                section="Constraint",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.LLM_JUDGE,
            ))
        return checks

    def _extract_anti_fabrication(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        lower = body.lower()
        has_af = (
            "anti-fabrication" in lower
            or "never fabricate" in lower
            or "do not fabricate" in lower
            or any(w in body for w in ANTI_FABRICATION_CN)
        )
        if not has_af:
            return checks
        for line in body.split("\n"):
            ls = line.strip().lower()
            if "fabricat" in ls or "invent" in ls or "hallucinat" in ls or any(w in line for w in ANTI_FABRICATION_CN):
                checks.append(InstructionCheck(
                    source=InstructionSource.ANTI_FABRICATION,
                    instruction_text=line.strip(),
                    section="Anti-Fabrication",
                    priority=InstructionPriority.REQUIRED,
                    check_method=CheckMethod.LLM_JUDGE,
                ))
        return checks

    def _extract_output_format(self, body: str) -> list[InstructionCheck]:
        # Bug B 修复:只在"输出/格式/template/result"标题下抽代码块当输出模板,
        # 否则 SKILL 里任意命令示例/JSON 片段都会被当"必须遵守的输出模板"→ audit 假 FAIL。
        # fence-aware:代码块内含 # 行(markdown 示例/注释)不算 heading,整块归属到
        # 它开 fence 时所处的 heading(不用 _split_sections——它 fence-naive 会把块切碎)。
        # 取舍:宁可漏抽(输出格式没放在匹配标题下 → 少几条 output 检查)也别过抽(假 FAIL)。
        checks: list[InstructionCheck] = []
        OUTPUT_HEADINGS = ("输出", "格式", "template", "result", "output", "format")
        current_heading = "Root"
        in_fence = False
        fence_heading = "Root"
        block_lines: list[str] = []
        per_section_count: dict[str, int] = {}
        for line in body.split("\n"):
            if line.lstrip().startswith("```"):
                if not in_fence:
                    in_fence = True
                    fence_heading = current_heading
                    block_lines = []
                else:
                    in_fence = False
                    h = fence_heading.lower()
                    if any(kw in h for kw in OUTPUT_HEADINGS) and per_section_count.get(fence_heading, 0) < 3:
                        per_section_count[fence_heading] = per_section_count.get(fence_heading, 0) + 1
                        checks.append(InstructionCheck(
                            source=InstructionSource.OUTPUT_FORMAT,
                            instruction_text="Output should follow template format",
                            section=fence_heading,
                            priority=InstructionPriority.RECOMMENDED,
                            check_method=CheckMethod.PATTERN_MATCH,
                            params={"sample": "\n".join(block_lines)[:200]},
                        ))
                continue
            if in_fence:
                block_lines.append(line)
            elif line.startswith("#"):
                current_heading = line.lstrip("#").strip()
        return checks

    def _extract_reference_usage(self, body: str) -> list[InstructionCheck]:
        checks: list[InstructionCheck] = []
        refs = re.findall(r"references/[\w\-\.]+|scripts/[\w\-\.]+", body)
        # 区分两类(P0-1d follow-up,修路由型 skill 假红灯 —— 见 §41 观察 1):
        # - 类别级 references/<X>(X 无扩展名 = 目录/类别):路由型 skill 的**互斥备选**,
        #   每任务只 1 类适用。合并成一条"查阅参考资料"(查任意 references/ 读)。
        #   否则 N 类互斥里 N-1 个不适用的会被算成假 FAIL —— ascendc-tiling-design
        #   曾 1/7 假红灯:softmax 属 reduction,Claude 只读 reduction 是对的,另 6 类
        #   本就不适用却算 FAIL,prog 率被虚低。
        # - 文件级(references/<X>.md 或 scripts/<X>):独立必读,各自一条查真 Read
        #   (读 1/3 不该算全过)。
        category_refs: set[str] = set()
        file_refs: set[str] = set()
        for ref in set(refs):
            seg = ref.split("/")[-1]
            if ref.startswith("references/") and "." not in seg:
                category_refs.add(ref)
            else:
                file_refs.add(ref)
        if category_refs:
            checks.append(InstructionCheck(
                source=InstructionSource.REFERENCE_USAGE,
                instruction_text=(
                    f"Consult reference material ({len(category_refs)} alternative category references)"
                ),
                section="References",
                priority=InstructionPriority.RECOMMENDED,
                # 路由型 skill 提取期无法知道任务属哪类,只查"有没有查参考"(反捏造语义)。
                # 读哪类由 Claude 路由决定;误路由(读错类)属内容/scoring,不是 compliance。
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"script_path": "references/"},
            ))
        for ref in file_refs:
            checks.append(InstructionCheck(
                source=InstructionSource.REFERENCE_USAGE,
                instruction_text=f"Use reference file: {ref}",
                section="References",
                priority=InstructionPriority.RECOMMENDED,
                # P0-1d: 查"Claude 真读了/调用了没",不是"文件在不在 output_dir"。
                # 后者因 P0-2 把 references 拷进考场而恒真、无意义。TOOL_CALL_CHECK +
                # script_path 让 _check_tool_call 匹配 tool_trace 入参里的该路径。
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"script_path": ref},
            ))
        return checks
