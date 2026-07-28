"""tests/test_eval_ids.py — run --eval-ids:按 id 指定某几道题跑 AB(精确选题)。

补 --eval-samples 的随机抽样——开发迭代常只需验证某几道(调试/回归/重现某道 fail)。
按 id 选(eval.id,int),逗号分隔;和 --eval-samples 互斥;id 不存在报错(列可用)。
"""
from __future__ import annotations

import pytest
import typer

from skill_eval.cli.helpers import _parse_eval_ids
from skill_eval.errors import SkillEvalError
from skill_eval.execution.runner import ABTestRunner
from skill_eval.models import EvalCase, RunConfig, ScoringConfig


def _evals(*ids: int) -> list[EvalCase]:
    return [EvalCase(id=i, prompt=f"task {i}") for i in ids]


def _runner(eval_ids=None, eval_samples=None) -> ABTestRunner:
    config = RunConfig(
        eval_ids=eval_ids,
        eval_samples=eval_samples,
        skip_compliance=True,
        skip_calibration=True,
    )
    return ABTestRunner(
        config=config,
        scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False),
    )


class TestParseEvalIds:
    def test_basic(self):
        assert _parse_eval_ids("3,5,7") == [3, 5, 7]

    def test_spaces(self):
        assert _parse_eval_ids("3, 5, 7") == [3, 5, 7]

    def test_empty_and_none(self):
        assert _parse_eval_ids("") is None
        assert _parse_eval_ids(None) is None

    def test_bad_format_raises(self):
        with pytest.raises(typer.BadParameter):
            _parse_eval_ids("3,a")


class TestSampleEvalsById:
    def test_select_by_id_preserves_given_order(self):
        # 按 --eval-ids 给的顺序(3,1),非 evals 原序(1,2,3)
        result = _runner(eval_ids=[3, 1])._sample_evals(_evals(1, 2, 3))
        assert [e.id for e in result] == [3, 1]

    def test_missing_id_raises_with_available(self):
        with pytest.raises(SkillEvalError) as exc:
            _runner(eval_ids=[99])._sample_evals(_evals(1, 2))
        msg = str(exc.value)
        assert "99" in msg
        assert "1" in msg and "2" in msg  # 报错要列出可用 id

    def test_dedup_preserves_order(self):
        result = _runner(eval_ids=[3, 3, 1])._sample_evals(_evals(1, 2, 3))
        assert [e.id for e in result] == [3, 1]

    def test_no_filter_returns_all(self):
        result = _runner()._sample_evals(_evals(1, 2, 3))
        assert [e.id for e in result] == [1, 2, 3]

    def test_eval_ids_takes_priority_in_runner(self):
        # runner 层:eval_ids 指定就走 id(防御,不校验互斥——互斥在 pipeline 层)
        result = _runner(eval_ids=[2], eval_samples=10)._sample_evals(_evals(1, 2, 3))
        assert [e.id for e in result] == [2]


class TestEvalIdsSamplesMutex:
    def test_pipeline_rejects_both(self, tmp_path):
        from skill_eval.cli.pipeline import _run_ab_eval
        from skill_eval.models import EvalSet

        eval_set = EvalSet(skill_name="s", evals=[EvalCase(id=1, prompt="x")])
        with pytest.raises(SkillEvalError):
            _run_ab_eval(
                skill_path=tmp_path / "skill", eval_set=eval_set, runs=1,
                model=None, workers=1, timeout=10, max_turns=1, scoring="hybrid",
                no_calibration=True, eval_ids=[1], eval_samples=2,
            )
