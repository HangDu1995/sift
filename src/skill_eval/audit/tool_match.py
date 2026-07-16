"""tool-call 匹配 + 平衡 JSON 抽取的低层 helper。

原 ``compliance.checker`` 的公共谓词,遵循性引擎统一到 audit 后迁此。audit(遵循性对账引擎)
与测试共用,无 skill_eval 依赖(纯 stdlib)。
"""

from __future__ import annotations

import re


def extract_first_balanced(text: str, open_ch: str = "[", close_ch: str = "]") -> str | None:
    """提取首个平衡的 JSON 数组/对象(字符串感知)。

    旧贪婪正则 ``\\[[\\s\\S]*\\]`` 会从首个 ``[`` 一路吞到文本**末个** ``]``。当 LLM
    在 JSON 之后的 reasoning 里用 ``[0]-[4]``/``[5]`` 引用条目(实测 GLM 常这么做),
    贪婪正则把 JSON+reasoning+尾 ``]`` 全包进来 → ``json.loads`` 失败 → 调用方默认
    全保留(适用性)或全 False(遵循),被误判成"LLM 漂移"(实为 parser bug,见探针)。

    本函数按括号深度配平取首个完整结构,且跳过 JSON 字符串内部的括号(evidence 里
    可能含 ``[N]``/``}``)。不平衡或不存在返回 None。
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# 公共名为 ``extract_first_balanced``;保留 ``_extract_first_balanced`` 别名(旧测试仍 import 它)。
_extract_first_balanced = extract_first_balanced


def _command_word_re(tool_name: str):
    """tool_name 的"命令词"匹配正则(词边界,大小写不敏感);tool_name 空则返回 None。

    供 ``match_tool_call`` 的 input_summary 命中判定。用 ``\\b`` 而非裸子串:tool_name
    须作为独立命令词出现在 input_summary 里,避免 ``git`` 命中 ``cd widgets``、``cat`` 命中
    ``concat.py`` 这类命令名词子串误判(假 PASS)。
    """
    if not tool_name:
        return None
    return re.compile(r"\b" + re.escape(tool_name) + r"\b", re.IGNORECASE)


def match_tool_call(tool_name: str, script_path: str, tool_trace) -> bool:
    """tool_trace 里是否存在匹配的调用,三条件任一命中即 True(大小写不敏感):

    1. ``tool_name`` 出现在某 ``tc.name``(子串,如 MCP 长工具名前缀
       ``webReader`` ∈ ``mcp__web_reader__webReader``)。
    2. ``script_path`` 出现在某 ``tc.input_summary``(子串,文件路径匹配)。
    3. ``tool_name`` 作为**命令词**出现在某 ``tc.input_summary``(**词边界**,非裸子串)。

    第 3 条是 Bash 子命令场景:SKILL.md「运行 pytest」抽出 tool_name=pytest,Claude 用 Bash
    跑该命令(input_summary='{"command": "pytest"}')。tc.name 是 "Bash" 而非 "pytest",条件 1
    漏判 → 假 FAIL;按词边界查 input_summary 命中。用词边界而非裸子串,避免 ``git`` 误命中
    ``cd widgets`` 这类命令名词子串(假 PASS)。

    tool_trace 元素支持 attr 风格(``tc.name``/``tc.input_summary``,如 ToolCallRecord)和
    dict 风格(``tc.get(...)``)两种。遍历完无命中 → False;空 / None tool_trace → False。
    """
    if not tool_trace:
        return False
    cmd_re = _command_word_re(tool_name)
    for tc in tool_trace:
        tc_name = tc.name if hasattr(tc, "name") else tc.get("name", "")
        tc_input = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
        if tool_name and tool_name.lower() in tc_name.lower():
            return True
        if script_path and script_path.lower() in tc_input.lower():
            return True
        if cmd_re and cmd_re.search(tc_input):
            return True
    return False
