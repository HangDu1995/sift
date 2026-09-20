"""时间统计(Phase 2.5):DurationAccumulator 线程安全累加秒数 + scoring 计时。"""

import math
import threading

from sift.execution.comparison_suite import ComparisonInputs, ScoringCallbacks
from sift.execution.runner import score_checkpoints
from sift.utils import DurationAccumulator


def test_duration_accumulator_adds_and_sums():
    acc = DurationAccumulator()
    acc.add(1.5)
    acc.add(2.5)
    assert math.isclose(acc.snapshot(), 4.0, rel_tol=0.0, abs_tol=0.0)


def test_duration_accumulator_none_or_zero_noop():
    acc = DurationAccumulator()
    acc.add(0.0)
    acc.add(None)
    assert math.isclose(acc.snapshot(), 0.0, rel_tol=0.0, abs_tol=0.0)  # 无有效时长


def test_duration_accumulator_concurrent_thread_safe():
    acc = DurationAccumulator()

    def worker():
        for _ in range(100):
            acc.add(0.01)

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert abs(acc.snapshot() - 8.0) < 1e-6  # 8×100×0.01,无丢失更新


def test_score_checkpoints_forwards_on_duration(tmp_path):
    """_score_checkpoints 在 grade 调用前后 perf_counter,经 on_duration 回灌时长。"""
    from unittest.mock import MagicMock, patch

    from sift.execution.runner import ABTestRunner, CheckpointContext
    from sift.models import EvalCase, GradingResult

    sink = []
    grade_resp = '{"expectations":[],"summary":{"passed":0,"failed":0,"total":0,"pass_rate":0.0}}'

    def fake_grade(self_grader, eval_case, transcript, output_dir, **k):
        return GradingResult.model_validate_json(grade_resp)

    runner = ABTestRunner.__new__(ABTestRunner)
    runner.scoring_config = MagicMock(enable_programmatic=False, enable_llm=True, llm_model=None, llm_timeout=10)
    ec = EvalCase(id=1, prompt="do", expectations=["e1"])  # expectations 非空才调 grade

    with patch("sift.scoring.llm_grader.LLMGrader.grade", fake_grade):
        score_checkpoints(runner.scoring_config, [ec], CheckpointContext("t", tmp_path, on_duration=sink.append))

    assert sink and all(isinstance(s, float) and s >= 0.0 for s in sink), "on_duration 没回灌 grade 调用时长(float>=0)"


def test_compare_with_retry_emits_duration():
    """_compare_with_retry 在 compare 调用前后 perf_counter,经 on_duration 回灌时长。"""
    import random
    from unittest.mock import patch

    from sift.execution.comparison_suite import _compare_with_retry
    from sift.models import ComparisonResult
    from sift.scoring.comparator import BlindComparator

    sink = []

    def fake_compare(self, comparison, **k):
        return ComparisonResult(winner="A", reasoning="t")  # not failed → 不重试

    comparator = BlindComparator(rng=random.Random(0))  # 真实例,patch compare 类方法才生效
    with patch("sift.scoring.comparator.BlindComparator.compare", fake_compare):
        _compare_with_retry(
            comparator=comparator,
            comparison=ComparisonInputs(output_a="a", output_b="b", eval_prompt="p", expectations=None),
            callbacks=ScoringCallbacks(on_duration=sink.append),
        )
    assert sink and all(isinstance(s, float) and s >= 0.0 for s in sink), "on_duration 没回灌 compare 调用时长"
