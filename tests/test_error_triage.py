"""失败三角化 classify_error 单元测。纯诊断层,不进判决。"""
from __future__ import annotations

from skill_eval.execution.error_triage import ErrorTriage, classify_error


class TestClassifyError:
    def test_cli_missing(self):
        t = classify_error("'claude' CLI not found — ensure it is installed and in PATH")
        assert t.root_cause == "cli_missing"

    def test_timeout(self):
        t = classify_error("claude -p timed out after 120s (killed process tree)")
        assert t.root_cause == "timeout"

    def test_timeout_chinese(self):
        t = classify_error("opencode export 超时")
        assert t.root_cause == "timeout"

    def test_max_turns(self):
        t = classify_error("error_max_turns: reached maximum turns")
        assert t.root_cause == "max_turns"

    def test_rate_limited_429(self):
        t = classify_error("exited with code 1: HTTP 429 rate_limit_error")
        assert t.root_cause == "rate_limited"

    def test_rate_limited_529_chinese(self):
        t = classify_error("HTTP 529 过载")
        assert t.root_cause == "rate_limited"

    def test_connection(self):
        t = classify_error("ConnectionRefusedError: [Errno 111] Connection refused")
        assert t.root_cause == "connection"

    def test_auth(self):
        t = classify_error("401 Unauthorized: invalid x-api-key token")
        assert t.root_cause == "auth"

    def test_session_not_found(self):
        t = classify_error("opencode: no session ses_xxx / id mismatch")
        assert t.root_cause == "session_not_found"

    def test_unknown_fallback_keeps_snippet(self):
        t = classify_error("some weird unexpected foobar baz qux")
        assert t.root_cause == "unknown"
        assert "foobar baz qux" in t.suggestion

    def test_empty_string_is_unknown(self):
        t = classify_error("")
        assert t.root_cause == "unknown"

    def test_first_match_wins(self):
        # 同时含 timeout 与 max_turns;timeout 在模式表先于 max_turns → timeout 赢
        t = classify_error("timed out after hitting max_turns")
        assert t.root_cause == "timeout"

    def test_every_cause_has_nonempty_suggestion(self):
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
