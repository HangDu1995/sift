"""失败三角化 classify_error 单元测。纯诊断层,不进判决。"""

from __future__ import annotations

from sift.execution.error_triage import (
    UNRESOLVED_CAUSE_INFO,
    ErrorTriage,
    classify_error,
    unresolved_cause_info,
)


class TestClassifyError:
    @staticmethod
    def test_cli_missing():
        t = classify_error("'claude' CLI not found — ensure it is installed and in PATH")
        assert t.root_cause == "cli_missing"

    @staticmethod
    def test_timeout():
        t = classify_error("claude -p timed out after 120s (killed process tree)")
        assert t.root_cause == "timeout"

    @staticmethod
    def test_timeout_chinese():
        t = classify_error("opencode export 超时")
        assert t.root_cause == "timeout"

    @staticmethod
    def test_max_turns():
        t = classify_error("error_max_turns: reached maximum turns")
        assert t.root_cause == "max_turns"

    @staticmethod
    def test_rate_limited_429():
        t = classify_error("exited with code 1: HTTP 429 rate_limit_error")
        assert t.root_cause == "rate_limited"

    @staticmethod
    def test_rate_limited_529_chinese():
        t = classify_error("HTTP 529 过载")
        assert t.root_cause == "rate_limited"

    @staticmethod
    def test_connection():
        t = classify_error("ConnectionRefusedError: [Errno 111] Connection refused")
        assert t.root_cause == "connection"

    @staticmethod
    def test_auth():
        t = classify_error("401 Unauthorized: invalid x-api-key token")
        assert t.root_cause == "auth"

    @staticmethod
    def test_session_not_found():
        t = classify_error("opencode: no session ses_xxx / id mismatch")
        assert t.root_cause == "session_not_found"

    @staticmethod
    def test_unknown_fallback_keeps_snippet():
        t = classify_error("some weird unexpected foobar baz qux")
        assert t.root_cause == "unknown"
        assert "foobar baz qux" in t.suggestion

    @staticmethod
    def test_empty_string_is_unknown():
        t = classify_error("")
        assert t.root_cause == "unknown"

    @staticmethod
    def test_first_match_wins():
        # 同时含 timeout 与 max_turns;timeout 在模式表先于 max_turns → timeout 赢
        t = classify_error("timed out after hitting max_turns")
        assert t.root_cause == "timeout"

    @staticmethod
    def test_every_cause_has_nonempty_suggestion():
        samples = {
            "cli_missing": "'claude' CLI not found in PATH",
            "timeout": "timed out after 60s",
            "max_turns": "error_max_turns reached",
            "rate_limited": "HTTP 429 throttled",
            "connection": "Connection refused",
            "auth": "401 Unauthorized token",
            "session_not_found": "no session ses_x",
        }
        for cause, text in samples.items():
            t = classify_error(text)
            assert t.root_cause == cause, f"{cause}: got {t.root_cause}"
            assert t.suggestion, f"{cause}: empty suggestion"


class TestUnresolvedCauseInfo:
    """UNRESOLVED cause → (标签, 建议) 展示映射(纯展示,不进判决)。"""

    @staticmethod
    def test_infra_causes_reuse_phase1_suggestion() -> None:
        # 基础设施 cause 复用第一期 classify_error 的 suggestion(非空)
        for cause in ("timeout", "rate_limited", "connection"):
            label, sug = unresolved_cause_info(cause)
            assert label  # 非空标签
            assert sug  # 复用了第一期 suggestion

    @staticmethod
    def test_structural_causes_present() -> None:
        assert "parse_failed" in UNRESOLVED_CAUSE_INFO
        assert "unmentioned" in UNRESOLVED_CAUSE_INFO
        assert "judge_error" in UNRESOLVED_CAUSE_INFO
        for c in ("parse_failed", "unmentioned", "judge_error"):
            label, sug = unresolved_cause_info(c)
            assert label and sug

    @staticmethod
    def test_unknown_cause_falls_back() -> None:
        label, sug = unresolved_cause_info("totally_unrecognized")
        assert label == "未知"
        assert "请查" in sug  # 兜底建议
