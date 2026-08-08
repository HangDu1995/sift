"""失败三角化:从 error 字符串抽根因 + 修复建议。

纯诊断/展示层——绝不进任何评测判决(pass_rate/win_rate/compliance/verdict 都不读 triage)。
只读 runner 已填的 error 字符串,加根因标签 + 修复建议。和 anomaly.py(时长维度)正交。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorTriage:
    root_cause: str
    suggestion: str


# 模式表:顺序匹配,首命中。(compiled_regex, root_cause, suggestion)
# 遇新 error 形态加一行即可。正则保守——宁可落 UNKNOWN 不误命中(误分类=给错建议,比不分类更糟)。
# 顺序有意:cli_missing(最具体的环境问题)→ timeout/max_turns(执行类)→ 网络类 → 兜底。
_PATTERNS: list[tuple["re.Pattern[str]", str, str]] = [
    (
        re.compile(r"CLI not found|not in PATH|not installed|binary 缺失", re.IGNORECASE),
        "cli_missing",
        "claude/opencode CLI 不在 PATH:确认已安装且在 PATH(终端/环境可见)",
    ),
    (
        re.compile(r"timed out|超时|killed process tree", re.IGNORECASE),
        "timeout",
        "执行超时:调大 --timeout / --max-turns,或任务过重需拆分",
    ),
    (
        re.compile(r"max_turns|max-turns|error_max_turns", re.IGNORECASE),
        "max_turns",
        "撞最大轮次:调大 --max-turns,或任务超出单轮容量",
    ),
    (
        re.compile(r"\b429\b|\b529\b|throttl|rate.?limit|过载|限流", re.IGNORECASE),
        "rate_limited",
        "端点限流:降并发(--max-concurrent)/重试/查客户端身份凭据(AI_AGENT+ENTRYPOINT)",
    ),
    (
        re.compile(r"refused|reset by peer|connection|ENOTFOUND|ECONN|unreachable", re.IGNORECASE),
        "connection",
        "连不上端点:查端点 URL / 网络 / DNS",
    ),
    (
        re.compile(r"\b401\b|\b403\b|unauthor|invalid.*token|认证失败|凭据", re.IGNORECASE),
        "auth",
        "认证失败:查 ANTHROPIC_AUTH_TOKEN / 凭据是否有效",
    ),
    (
        re.compile(r"no session|session.*not|id.*mismatch|无 session|id 不匹配", re.IGNORECASE),
        "session_not_found",
        "找不到 session:检查 session id / opencode session list 确认",
    ),
]


def classify_error(error_str: str) -> ErrorTriage:
    """从 error 字符串抽根因 + 修复建议。无命中 → UNKNOWN(不假装分类)。"""
    if not error_str:
        return ErrorTriage(root_cause="unknown", suggestion="(空 error)请查完整日志")
    for pattern, cause, suggestion in _PATTERNS:
        if pattern.search(error_str):
            return ErrorTriage(root_cause=cause, suggestion=suggestion)
    snippet = error_str[:200].replace("\n", " ")
    return ErrorTriage(
        root_cause="unknown",
        suggestion=f"(未识别根因)原文: {snippet} — 请查完整日志 / grading.json / error.txt",
    )
