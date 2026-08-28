"""audit 越界视图(一期:领域工具对账,信息性)——轨迹动作 × 声明 的对偶面。

覆盖视图回答「声明的指令被触达了吗」;本模块回答它的对偶:**「轨迹里的动作都有
声明对应吗」**——补「完全未声明且未划边界的行为无指令可对账」的诚实盲区。一期只做
其中一型:**未声明领域工具**(undeclared-domain)。纯聚合、零 LLM、零缓存(每次
全量重算,秒级),**不产 finding、不进 verdict**——SKILL.md 通常不穷举工具
(bash/read 等不在声明里),未匹配 ≠ 违规,基线未成熟前判罚必乱(宁少报不乱报);
此视图是二期 per-skill 行为基线的数据采集面。

**工具分类**(一律 ``name.lower() in BUILTIN_TOOLS`` 判内置——canonical 工具名
双形:cc 记录原样大写开头、opencode 经 ``_norm_tool`` 归一的未命中名小写透传,
lower 统一口径,不依赖只覆盖 6 名的 ``transcript._NAME_MAP``):

- ``builtin``:内置工具(cc/opencode 自带的探索/编辑原语 + agent-insight 同思路的
  non_business 白名单:过渡性操作不构成越界信号),**不进视图**;
- ``domain``:内置表外 + 命中声明工具要求 → 「已声明」,列出但不标警;
- ``undeclared-domain``:内置表外 + 未命中 → **视图主体**(信息性标注,不判 FAIL)。

内置表是**数据不是逻辑**(cc/opencode 合一初版;表外内置工具会被保守地列进
undeclared-domain,人看一眼即可补表);**Skill 调用本身不算动作**(skill 进入信号,
归 slice 层,builtin 表含 skill 自然跳过)。声明侧抽取:TOOL_REQUIREMENT 指令自带
``params.tool_name``(extractor 已抽好,直接用);另扫全部指令文本里的
``mcp__[server]__[tool]`` 全名与反引号代码字(约束/禁令里划边界的 MCP 工具)。
抽不出 → 空(该 skill 无工具声明 → 全部 builtin 外工具进 undeclared-domain,视图头
注明防误读)。第四源(声明文件 frontmatter):agent 声明的 ``tools:`` 白名单(cc 强制
生效)与 skill 的 ``allowed-tools:`` ——由调用方(CLI/runner,拿得到 frontmatter)抽好
传入,引擎层不读文件。
"""
from __future__ import annotations

import re

from sift.models.audit_models import BoundarySummary, ToolUsageEntry
from sift.models.batch_models import InstructionCheck
from sift.models.enums import InstructionSource

# 内置工具表(全小写;cc + opencode 合一初版)。表 = 数据不是逻辑:新增内置工具漏在
# 表外会被保守列进 undeclared-domain(信息性),补一行即可。
# 含 agent-insight 同思路的 non_business 白名单(grep/读文件/env init 等过渡操作)。
BUILTIN_TOOLS: frozenset[str] = frozenset({
    # cc / opencode 通用原语
    "read", "write", "edit", "multiedit", "notebookedit",
    "bash", "glob", "grep",
    # 子代理 / 计划 / 交互
    "task", "agent", "ask", "askuserquestion", "exitplanmode", "plan", "stop",
    "todowrite", "todo",
    # web / 知识
    "webfetch", "websearch",
    # skill 进入信号(slice 层,不算动作)
    "skill", "slashcommand",
})

# mcp 全名:mcp__<server>__<tool>(server/tool 可含字母数字下划线连字符)。
_MCP_FULL_NAME_RE = re.compile(r"\bmcp__[A-Za-z0-9_\-]+__[A-Za-z0-9_\-]+")

# 反引号代码字:`Grep` / `npu-smi` / `parse_result.py` 等(声明文本里显式指名的工具/脚本)。
_BACKTICK_TOKEN_RE = re.compile(r"`([A-Za-z][\w\-\.]*)`")


def frontmatter_tool_names(frontmatter: dict) -> dict[str, str]:
    """从声明 frontmatter 抽工具白名单 → ``{规范化名(lower): 显示文本}``。

    认两个字段:agent 声明的 ``tools:``(cc 强制生效的白名单)、skill 的
    ``allowed-tools:``。值必须是 str 列表(别的形态静默忽略——YAML 手写体多样,
    宁可漏声明也不猜);builtin 名收进来无害(builtin 判定在前)。无命中 → 空 dict。
    """
    out: dict[str, str] = {}
    for field in ("tools", "allowed-tools"):
        val = frontmatter.get(field)
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, str):
                continue
            key = item.strip().lower()
            if key and key not in out:
                out[key] = f"frontmatter: {field}"
    return out


def declared_tool_names(
    instructions: list[InstructionCheck],
    declared_tools: dict[str, str] | None = None,
) -> dict[str, str]:
    """从声明指令抽工具名 → ``{规范化名(lower): 显示文本}``。

    四个来源(保守并集,宁多列进 undeclared 也不静默吞——信息性视图的方向安全):
    1. ``TOOL_REQUIREMENT.params.tool_name``(extractor 已抽好,最可靠);
    2. 任意指令文本里的 ``mcp__server__tool`` 全名(约束/禁令划边界的 MCP 工具);
    3. 任意指令文本里的反引号代码字(`` `Grep` `` 等;文件名/脚本名收进来也无害——
       它们不匹配任何 tool_use 名,不影响 undeclared 计数);
    4. ``declared_tools``(调用方从 frontmatter ``tools:``/``allowed-tools:`` 抽好的
       白名单,见 ``frontmatter_tool_names``;并集兜底——正文指令的显示文本优先)。
    显示文本取 headline > instruction_text(first-wins)。无工具声明 → 空 dict。
    """
    declared: dict[str, str] = {}

    def _add(name: str, display: str) -> None:
        key = (name or "").strip().lower()
        if key and key not in declared:
            declared[key] = display

    for inst in instructions:
        display = inst.headline or inst.instruction_text
        if inst.source == InstructionSource.TOOL_REQUIREMENT:
            _add(str(inst.params.get("tool_name") or ""), display)
        text = f"{inst.instruction_text}\n{inst.source_excerpt}"
        for m in _MCP_FULL_NAME_RE.finditer(text):
            _add(m.group(0), display)
        for m in _BACKTICK_TOKEN_RE.finditer(text):
            _add(m.group(1), display)
    for key, display in (declared_tools or {}).items():
        _add(key, display)
    return declared


def aggregate_boundary(
    tool_traces: list[tuple[str, list]],
    instructions: list[InstructionCheck],
    declared_tools: dict[str, str] | None = None,
) -> BoundarySummary:
    """分类 + 对账 + 聚合出 BoundarySummary(纯聚合,零 LLM)。

    Args:
        tool_traces: ``[(transcript 名, rebuild 的 tool_trace), ...]`` —— rebuild 产物
            (cc/opencode 同形;名字大小写双形由 lower 统一)。
        instructions: 声明侧指令(refine 后;derived 反推指令不传——拿被审对象自己
            声明自己会造成循环论证)。
        declared_tools: frontmatter 工具白名单(``frontmatter_tool_names`` 产;引擎层
            不读文件,由调用方传入)。并集兜底——正文指令声明优先。

    排序:undeclared-domain 先(sessions 降序,再 calls 降序),再 domain(calls 降序);
    空输入 → 全 0。builtin 不进 ``tools``(不构成越界信号)。
    """
    declared = declared_tool_names(instructions, declared_tools)
    entries: dict[str, ToolUsageEntry] = {}

    for transcript_name, trace in tool_traces:
        seen_in_session: set[str] = set()
        for call in trace:
            name = (getattr(call, "name", "") or "").strip()
            if not name:
                continue
            key = name.lower()
            if key in BUILTIN_TOOLS:
                continue  # 内置原语/白名单:不是动作,不进视图
            if key in seen_in_session:
                entries[key].calls += 1
                continue
            seen_in_session.add(key)
            if key not in entries:
                hit = declared.get(key)
                entries[key] = ToolUsageEntry(
                    name=name,
                    classification="domain" if hit else "undeclared-domain",
                    declared_by=hit or "",
                    sessions=0,
                    calls=0,
                    transcripts=[],
                )
            entries[key].sessions += 1
            entries[key].calls += 1
            if transcript_name not in entries[key].transcripts:
                entries[key].transcripts.append(transcript_name)

    tools = list(entries.values())
    undeclared = [e for e in tools if e.classification == "undeclared-domain"]
    domain = [e for e in tools if e.classification == "domain"]
    undeclared.sort(key=lambda e: (-e.sessions, -e.calls, e.name))
    domain.sort(key=lambda e: (-e.calls, e.name))
    return BoundarySummary(
        total_tools=len(tools),
        declared=len(domain),
        undeclared=len(undeclared),
        tools=undeclared + domain,
    )
