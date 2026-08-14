"""tool-call 匹配 + 平衡 JSON 抽取的低层 helper。

原 ``compliance.checker`` 的公共谓词,遵循性引擎统一到 audit 后迁此。audit(遵循性对账引擎)
与测试共用,无 sift 依赖(纯 stdlib)。
"""

from __future__ import annotations

import json
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


# 尾逗号容错:LLM 长批 JSON 偶发 [...,] / {...,} → json.loads 硬失败。
# 仅在直接 parse 失败后才 sub(不在所有输入上跑,以免破坏含 ", ]" 的合法字符串值)。
_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")


def load_json_lenient(s: str):
    """``json.loads`` + 尾逗号容错;parse 不出返回 None。

    LLM(尤其 GLM / 类 Claude)长批 JSON 输出偶发尾逗号(``[..., ]`` / ``{...,}``)→
    ``json.loads`` 硬失败 → 调用方整批回退默认(没判成 → UNRESOLVED;audit 模式下是主
    交付物有洞)。两阶段防御(对齐 DeepEval ``trimAndLoadJson``):

    1. 先直接 ``json.loads``——干净输入(含合法 ``", ]"`` 字符串值)直接成功,不跑正则;
    2. 仅在直接失败后才 ``re.sub`` 去掉 ``]`` / ``}`` 前的尾逗号重试一次。

    两阶段都失败 → None(调用方兜底默认)。极少数"既尾逗号、字符串值内又含 `,]`"的病态
    输入,第 2 步正则可能改写该字符串值——与 DeepEval 同限,可接受(尾逗号本身已非合法)。
    """
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        try:
            return json.loads(_TRAILING_COMMA_RE.sub(r"\1", s))
        except json.JSONDecodeError:
            return None


def _command_word_re(tool_name: str):
    """tool_name 的"命令词"匹配正则(词边界,大小写不敏感);tool_name 空则返回 None。

    供 ``match_tool_call`` 的 input_summary 命中判定。用 ``\\b`` 而非裸子串:tool_name
    须作为独立命令词出现在 input_summary 里,避免 ``git`` 命中 ``cd widgets``、``cat`` 命中
    ``concat.py`` 这类命令名词子串误判(假 PASS)。
    """
    if not tool_name:
        return None
    return re.compile(r"\b" + re.escape(tool_name) + r"\b", re.IGNORECASE)


# input 里常见含路径的字段(Read/Write/Edit → file_path;Grep/Glob → path)
_PATH_FIELDS = ("file_path", "path")


def _norm_path(p: str) -> str:
    """路径词法规范化:\\→/、压双斜杠、去 ``./`` 前缀与 ``/./`` 段、lower。

    不解析 ``..``(语义依赖真实文件系统,留 LLM);只做安全的词法规范化供匹配。
    """
    if not p:
        return ""
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    p = p.replace("/./", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.lower()


def _path_matches(script_path: str, tc) -> bool:
    """script_path 是否匹配 tc 的路径。

    优先结构化 ``tc.input`` 的路径字段,规范化后用**分隔符边界**比对(``==`` 或
    ``endswith("/"+sp)``)——同时解相对/绝对差异(``/workspace/r.md`` 命中 ``r.md``)和
    前缀边界(``/not-r.md`` 不被 ``r.md`` 误命中,比裸子串多修一个 bug)。无结构化
    input(dict 风格 / 旧数据 / ``input=None``)→ 回退子串(input_summary),向后兼容。
    """
    sp = _norm_path(script_path)
    if not sp:
        return False
    tc_input = tc.input if hasattr(tc, "input") else tc.get("input")
    if isinstance(tc_input, dict):
        for field in _PATH_FIELDS:
            val = tc_input.get(field)
            if isinstance(val, str) and val:
                fp = _norm_path(val)
                if fp == sp or fp.endswith("/" + sp):
                    return True
        return False  # 有结构化 input 但路径字段不匹配 → 精确化(不回退子串歪打正着)
    tc_summary = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
    return sp in (tc_summary or "").lower()


def _params_satisfied(required_params, tc) -> bool:
    """每个 required_param 都在 tc 的 command(优先结构化 input)或 input_summary 里出现(子串)。

    粗召回(宁松):``--tb=short`` 整串、positional token(``tests/``)都算。全满足才 True;
    任一缺 → False(该 tc 不命中,走 LLM 兜底)。空 required_params → True(无要求)。
    """
    params = [p for p in (required_params or []) if p]
    if not params:
        return True
    tc_input = tc.input if hasattr(tc, "input") else tc.get("input")
    cmd = ""
    if isinstance(tc_input, dict):
        cmd = str(tc_input.get("command", "") or "")
    if not cmd:
        cmd = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
    cmd = (cmd or "").lower()
    return all(p.lower() in cmd for p in params)


def match_tool_call(tool_name: str, script_path: str, tool_trace, required_params=None) -> bool:
    """tool_trace 里是否存在匹配的调用(大小写不敏感)。

    三条件:
    1. ``tool_name`` 出现在某 ``tc.name``(子串,如 MCP 长名前缀
       ``webReader`` ∈ ``mcp__web_reader__webReader``)。
    2. ``script_path`` 匹配某 ``tc`` 的路径(``_path_matches``:优先结构化 ``tc.input`` 的
       file_path/path 字段,规范化 + 分隔符边界;无结构化 input → 回退子串)。
    3. ``tool_name`` 作为**命令词**出现在某 ``tc.input_summary``(**词边界**,Bash 子命令场景)。

    ``required_params``(可选 list[str]):工具命中后额外断言每个参数 token(如 ``--tb=short``)
    都在 ``command``(优先结构化 ``tc.input["command"]``,无则 input_summary)里出现。全满足才
    True;任一缺 → 该 tc 不命中(走 LLM 兜底,即「工具名对、参数没照做」不放过)。None/空 →
    旧行为(三条件任一命中即 True)。参数级断言只配套工具命中(条件 1/3),不与路径(条件 2,
    REFERENCE_USAGE)同时出现。

    tool_trace 元素支持 attr 风格(``tc.name``/``tc.input``/``tc.input_summary``,如
    ToolCallRecord)和 dict 风格(``tc.get(...)``)。遍历完无命中 → False;空/None tool_trace → False。
    """
    if not tool_trace:
        return False
    cmd_re = _command_word_re(tool_name)
    for tc in tool_trace:
        tc_name = tc.name if hasattr(tc, "name") else tc.get("name", "")
        tc_input = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
        name_hit = bool(tool_name) and tool_name.lower() in tc_name.lower()
        cmd_hit = bool(cmd_re) and bool(cmd_re.search(tc_input))
        if required_params:
            # 有参数要求:命中工具(名/命令词)+ 所有参数满足才 True
            if (name_hit or cmd_hit) and _params_satisfied(required_params, tc):
                return True
            continue
        if name_hit:
            return True
        if script_path and _path_matches(script_path, tc):
            return True
        if cmd_hit:
            return True
    return False
