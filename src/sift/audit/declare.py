"""declare: 从 transcript digest 反推"真实使用里确实需要的"指令。

§48 杠杆:SKILL.md extractor 可能漏抽指令,transcript 真实体现的补充上。
**只增不减**——本函数返回的 list 是**纯新增**(去重 vs existing),不含 existing
本身。auditor 负责 existing + derived 合并,并把 derived 的 finding 标
``derived_from_transcript=True``。

v1 反推范围(聚焦能干净映射的):
- **required_reads(必做)**: ``digest.input_files`` 每个 ``f.path`` → 一条
  ``InstructionCheck``(source=REFERENCE_USAGE, params={"script_path": path},
  check_method=TOOL_CALL_CHECK)。input_files 是 transcript 里被 Read 且未改写的
  文件,即 agent 真实读过的输入/参考文件 → 干净映射成"应查阅"指令。
- **required_tools(留 v2,不做)**: 见下方 ``_TOOLS_NOT_DERIVED``。

去重键:``(source, params.script_path)``。existing 已有同 source + 同 script_path
的 REFERENCE_USAGE → 该 path 不重复加。
"""
from __future__ import annotations

from sift.evals.transcript import SessionDigest
from sift.models import InstructionCheck
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)

# required_tools / required_outputs 不反推的理由:
#
# required_tools: 候选源有二,均映射不干净——
#   1) ``digest.commands``(Bash cmd 字符串): 一条 cmd 常串联多命令(管道/&&/;),
#      且 python/git/ls/cat/pip/npm 等通用 shell 命令与 skill 真正要求的业务命令
#      (如 scripts/train.py)无法可靠区分。强行提取会把通用 shell 命令塞成
#      TOOL_REQUIREMENT —— 恰是"硬塞没意义的反推"。
#   2) ``digest.tool_trace``(工具名): 过滤掉通用文件工具
#      (Read/Write/Edit/MultiEdit/Glob/Grep/Skill/Bash)后,剩下的也是
#      WebSearch/Task 这类基础设施工具,无一是 skill 要求的具体业务命令。
#      TOOL_CALL_CHECK 按 tool_name 匹配 tc.name,把 "Bash" 当业务命令会命中所有
#      Bash 调用,无意义。
#   → 业务命令提取不确定,留 v2(需先定义"业务命令"的干净规则 + stoplist)。
#
# required_outputs(produced_files): 无对应指令类型。FILE_PRESENCE 查活文件系统,
# 历史 transcript 审计无意义(transcript 里 produced 的文件未必仍在磁盘);且
# "产出文件检查"语义属 scoring 断言,不属 compliance 指令。不反推。
_TOOLS_NOT_DERIVED = True  # 占位标记,文档性常量(见上方注释)


def derive_declarations(
    digest: SessionDigest,
    existing: list[InstructionCheck],
) -> list[InstructionCheck]:
    """从 transcript digest 反推**新增的**指令声明(只增不减)。

    Args:
        digest: 已重建的 session digest(含 input_files 等)。
        existing: SKILL.md extractor 当前抽出的指令全集(用于去重)。

    Returns:
        纯新增的 ``InstructionCheck`` 列表(去重 vs existing 后的),
        **不含** existing 本身。空 digest → 空 list。

    反推范围(v1): 仅 required_reads(input_files → REFERENCE_USAGE)。
    required_tools / required_outputs 不反推,理由见模块 docstring。
    """
    # existing 里 REFERENCE_USAGE 的 script_path 集合(去重键的右半边)
    existing_read_paths: set[str] = {
        c.params.get("script_path")
        for c in existing
        if c.source == InstructionSource.REFERENCE_USAGE
        and c.params.get("script_path")
    }

    derived: list[InstructionCheck] = []
    seen: set[str] = set()  # 同一 input_files 内重复 path 也去重(防御性)
    for f in digest.input_files:
        path = f.path
        if not path or path in existing_read_paths or path in seen:
            continue
        seen.add(path)
        derived.append(InstructionCheck(
            source=InstructionSource.REFERENCE_USAGE,
            instruction_text=f"读取 {path}",
            section="transcript-derived",
            priority=InstructionPriority.RECOMMENDED,
            check_method=CheckMethod.TOOL_CALL_CHECK,
            params={"script_path": path},
        ))
    return derived
