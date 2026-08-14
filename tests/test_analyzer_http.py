"""P3:analyzer 切 http(forced tool_use + schema)的专项测试。"""
from unittest.mock import patch

from sift.concurrency import _LLMRetryExhausted
from sift.scoring.analyzer import ANALYSIS_SCHEMA, PosthocAnalyzer


class TestAnalysisSchema:
    def test_schema_keeps_all_properties(self):
        # AnalysisResult 全字段都是真分析产出,无 internal 可剔。
        props = ANALYSIS_SCHEMA["properties"]
        for kept in ("winner_strengths", "loser_weaknesses", "instruction_following",
                     "improvement_suggestions", "transcript_insights"):
            assert kept in props

    def test_schema_inlined_no_ref_no_defs(self):
        # 同 grader:$ref 经 inline_schema 展开(部分端点不解析 $ref → 空响应 → None)。
        import json
        assert "$defs" not in ANALYSIS_SCHEMA
        assert '"$ref"' not in json.dumps(ANALYSIS_SCHEMA)

    def test_schema_required_empty_all_optional(self):
        assert ANALYSIS_SCHEMA.get("required", "MISSING") == []


class TestAnalyzerCallHttp:
    @staticmethod
    def _fake(response):
        captured = []

        def _f(prompt, *a, **k):
            captured.append(k)
            return response

        return _f, captured

    def test_call_uses_http_transport(self):
        fake, cap = self._fake('["note"]')
        with patch("sift.scoring.analyzer.call_llm_with_retry", side_effect=fake):
            PosthocAnalyzer()._call("prompt", schema=ANALYSIS_SCHEMA)
        assert cap[0]["transport"] == "http"

    def test_call_returns_empty_on_retry_exhausted(self):
        with patch("sift.scoring.analyzer.call_llm_with_retry",
                   side_effect=_LLMRetryExhausted("exhausted")):
            assert PosthocAnalyzer()._call("prompt") == ""

    def test_analyze_comparison_passes_analysis_schema(self):
        fake, cap = self._fake('{}')
        with patch("sift.scoring.analyzer.call_llm_with_retry", side_effect=fake):
            PosthocAnalyzer().analyze_comparison(
                "A", "winner", "loser", "reasoning", "wt", "lt", focus_skill="f")
        assert cap[0]["schema"] is ANALYSIS_SCHEMA
