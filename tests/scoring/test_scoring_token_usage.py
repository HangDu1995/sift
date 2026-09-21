"""Phase 2: scoring 三类(grader/comparator/analyzer)的 on_usage 转发。

mock 各类模块内绑定的 call_claude 本地名,回灌 on_usage payload,断言公开方法
把 on_usage 穿到底层 call_claude。默认 None → 行为不变(回归)。
"""

import random
from unittest.mock import patch

from sift.execution.comparison_suite import ScoringCallbacks
from sift.execution.runner import score_checkpoints
from sift.scoring.analyzer import PosthocAnalyzer
from sift.scoring.comparator import BlindComparator
from sift.scoring.llm_grader import LLMGrader
from sift.scoring.prompts import ComparisonInputs


def _usage_payload():
    return {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 2,
        "total_cost_usd": 0.001,
    }


def _fake_that_reports_usage(response_text: str):
    """返回 side_effect:调 call_claude 时若有 on_usage 就回灌 payload,返回 response_text。"""
    captured = []

    def _f(prompt, *a, **k):
        ou = k.get("on_usage")
        captured.append(ou)
        if ou:
            ou(_usage_payload())
        return response_text

    return _f, captured


def test_grader_grade_forwards_on_usage(tmp_path):
    from sift.models import EvalCase

    ec = EvalCase(id=1, prompt="do")
    sink = []
    fake, cap = _fake_that_reports_usage(
        '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}'
    )
    with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
        LLMGrader().grade(ec, "t", tmp_path, on_usage=sink.append)
    assert cap and cap[0] is not None, "grade 没把 on_usage 穿到 call_claude"
    assert sink == [_usage_payload()], "on_usage payload 没回灌"


def test_comparator_compare_forwards_on_usage():
    sink = []
    fake, cap = _fake_that_reports_usage('{"winner":"A","reasoning":"t"}')
    with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
        BlindComparator(rng=random.Random(0)).compare(
            on_usage=sink.append, comparison=ComparisonInputs(output_a="a", output_b="b", eval_prompt="p")
        )
    assert cap and cap[0] is not None, "compare 没把 on_usage 穿到 call_claude"
    assert sink == [_usage_payload()]


def test_analyzer_analyze_benchmark_forwards_on_usage():
    sink = []
    fake, cap = _fake_that_reports_usage('["note"]')
    with patch("sift.scoring.client.call_llm_with_retry", side_effect=fake):
        PosthocAnalyzer().analyze_benchmark("{}", "", on_usage=sink.append)
    assert cap and cap[0] is not None, "analyze_benchmark 没把 on_usage 穿到 call_claude"
    assert sink == [_usage_payload()]


def test_score_checkpoints_forwards_on_usage_to_grader(tmp_path):
    """_score_checkpoints 接 on_usage 并穿到 grader.grade(非 programmatic 路径)。"""
    from unittest.mock import MagicMock

    from sift.execution.runner import ABTestRunner, CheckpointContext
    from sift.models import EvalCase, GradingResult

    sink = []
    grade_resp = '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}'

    def fake_grade(self_grader, eval_case, transcript, output_dir, **k):
        ou = k.get("on_usage")
        sink.append(("grade", ou))
        if ou:
            ou({"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.002})
        return GradingResult.model_validate_json(grade_resp)

    runner = ABTestRunner.__new__(ABTestRunner)  # 不走 __init__
    runner.scoring_config = MagicMock(enable_programmatic=False, enable_llm=True, llm_model=None, llm_timeout=10)
    ec = EvalCase(id=1, prompt="do", expectations=["e1"])  # expectations 非空才调 grade

    with patch("sift.scoring.llm_grader.LLMGrader.grade", fake_grade):
        score_checkpoints(
            runner.scoring_config, [ec], CheckpointContext("t", tmp_path, on_usage=lambda p: sink.append(("cb", p)))
        )

    # on_usage 穿到 grade(非 None)+ payload 回灌到外层 callback
    assert any(tag == "grade" and ou is not None for tag, ou in sink), "on_usage 没穿到 grade"
    assert any(
        tag == "cb" and p == {"input_tokens": 7, "output_tokens": 3, "total_cost_usd": 0.002} for tag, p in sink
    ), "payload 没回灌到外层 callback"


def test_compare_with_retry_forwards_on_usage():
    """_compare_with_retry 接 on_usage 并穿到 comparator.compare。"""

    from sift.execution.comparison_suite import _compare_with_retry
    from sift.models import ComparisonResult

    captured = []  # 记 on_usage 是否传(非 None)
    sink = []  # 记 payload

    def fake_compare(self, comparison, **k):
        captured.append(k.get("on_usage"))
        ou = k.get("on_usage")
        if ou:
            ou({"input_tokens": 5, "output_tokens": 2, "total_cost_usd": 0.001})
        return ComparisonResult(winner="A", reasoning="t")  # not failed → 不重试

    comparator = BlindComparator(rng=random.Random(0))  # 真实例,patch 其 compare 类方法才生效
    with patch("sift.scoring.comparator.BlindComparator.compare", fake_compare):
        _compare_with_retry(
            comparator=comparator,
            comparison=ComparisonInputs(output_a="a", output_b="b", eval_prompt="p", expectations=None),
            callbacks=ScoringCallbacks(on_usage=sink.append),
        )
    assert any(ou is not None for ou in captured), "on_usage 没穿到 compare"
    assert sink == [{"input_tokens": 5, "output_tokens": 2, "total_cost_usd": 0.001}], "payload 没回灌"


def test_run_analysis_forwards_on_usage(tmp_path):
    """run_analysis 接 on_usage 并穿到 analyzer.analyze_benchmark。"""
    from sift.execution.analysis_suite import run_analysis
    from sift.execution.benchmark import BenchmarkAggregator
    from sift.models import BenchmarkData, BenchmarkMetadata
    from sift.models.benchmark_models import RunResultEntry, RunResultMetrics

    captured = []
    sink = []

    def fake_analyze(self, benchmark_json, skill_description="", **k):
        captured.append(k.get("on_usage"))
        ou = k.get("on_usage")
        if ou:
            ou({"input_tokens": 8, "output_tokens": 4, "total_cost_usd": 0.003})
        return ["note"]

    agg = BenchmarkAggregator(model=None)
    bench = BenchmarkData(
        metadata=BenchmarkMetadata(skill_name="s"),
        runs=[
            RunResultEntry(
                eval_id=1,
                configuration="with_skill",
                run_number=1,
                result=RunResultMetrics(pass_rate=1.0, passed=1, failed=0, total=1),
            )
        ],
    )
    with patch("sift.scoring.analyzer.PosthocAnalyzer.analyze_benchmark", fake_analyze):
        run_analysis(agg, tmp_path, bench, on_usage=sink.append)
    assert any(ou is not None for ou in captured), "on_usage 没穿到 analyze_benchmark"
    assert sink == [{"input_tokens": 8, "output_tokens": 4, "total_cost_usd": 0.003}], "payload 没回灌"
