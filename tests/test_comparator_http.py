"""P2:comparator 切 http(forced tool_use + schema)的专项测试。

schema 形状(确定性,主风险点)+ _call http 行为(transport/schema/on_usage/
retry-exhaustion 降级)+ L1(call_llm_with_retry)↔ L2(_compare_with_retry)分层。
"""
from unittest.mock import patch

from skill_eval.concurrency import _LLMRetryExhausted
from skill_eval.scoring.comparator import BlindComparator, COMPARATOR_SCHEMA


class TestComparatorSchema:
    def test_schema_excludes_internal_fields(self):
        props = COMPARATOR_SCHEMA["properties"]
        for internal in ("failed", "pos_a_label", "pos_b_label"):
            assert internal not in props
        for llm_field in ("winner", "reasoning", "rubric", "output_quality", "expectation_results"):
            assert llm_field in props

    def test_schema_keeps_defs_to_avoid_dangling_ref(self):
        assert "$defs" in COMPARATOR_SCHEMA
        assert len(COMPARATOR_SCHEMA["$defs"]) == 6

    def test_schema_required_empty_all_fields_have_defaults(self):
        assert COMPARATOR_SCHEMA.get("required", "MISSING") == []

    def test_schema_conformant_json_validates(self):
        import json
        from skill_eval.models import ComparisonResult
        sample = json.dumps({"winner": "A", "reasoning": "x", "rubric": {}, "output_quality": {}})
        r = ComparisonResult.model_validate(json.loads(sample))
        assert r.winner == "A"


class TestComparatorCallHttp:
    @staticmethod
    def _fake(response):
        captured = []

        def _f(prompt, *a, **k):
            captured.append(k)
            return response

        return _f, captured

    def test_call_uses_http_transport_and_comparator_schema(self):
        fake, cap = self._fake('{"winner":"A","reasoning":"t"}')
        with patch("skill_eval.scoring.comparator.call_llm_with_retry", side_effect=fake):
            BlindComparator()._call("prompt")
        assert cap[0]["transport"] == "http"
        assert cap[0]["schema"] is COMPARATOR_SCHEMA

    def test_call_returns_empty_on_retry_exhausted(self):
        with patch("skill_eval.scoring.comparator.call_llm_with_retry",
                   side_effect=_LLMRetryExhausted("exhausted")):
            assert BlindComparator()._call("prompt") == ""

    def test_call_forwards_on_usage(self):
        fake, cap = self._fake('{"winner":"A","reasoning":"t"}')
        with patch("skill_eval.scoring.comparator.call_llm_with_retry", side_effect=fake):
            BlindComparator()._call("prompt", on_usage=lambda u: None)
        assert cap[0]["on_usage"] is not None

    def test_L1_exhaustion_flows_to_L2_pair_retry(self):
        import random
        from skill_eval.execution.comparison_suite import _compare_with_retry
        valid = '{"winner":"A","reasoning":"ok","rubric":{},"output_quality":{}}'
        comparator = BlindComparator(rng=random.Random(0))
        with patch("skill_eval.scoring.comparator.call_llm_with_retry",
                   side_effect=[_LLMRetryExhausted("boom"), valid]), \
                patch("skill_eval.execution.comparison_suite.time"):
            result = _compare_with_retry(comparator, "a", "b", "p", None, max_retries=3)
        assert result is not None
        assert result.winner in ("with", "without")
        assert result.failed is False
