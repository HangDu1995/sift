from skill_eval.evals.inspect import inspect_evals, PROGRAMMATIC_TYPES
from skill_eval.models import Assertion, EvalCase, EvalSet
from skill_eval.models.enums import AssertionType


def _set(evals):
    return EvalSet(skill_name="sk", evals=evals)


def test_completeness_and_stability_counts():
    e = EvalCase(
        id=1, prompt="p",
        expectations=["exp1", "exp2"],
        assertions=[
            Assertion(name="a", type=AssertionType.FILE_EXISTS),
            Assertion(name="b", type=AssertionType.CONTAINS_PATTERN),
            Assertion(name="c", type=AssertionType.LLM_GRADE),
        ],
        level="L1",
    )
    r = inspect_evals(_set([e]))
    assert r["completeness"] == {"evals": 1, "assertions": 3, "expectations": 2, "levels": {"L1": 1}}
    assert r["stability"]["programmatic"] == 2
    assert r["stability"]["llm"] == 1
    assert r["stability"]["programmatic_ratio"] == 2 / 3
    assert r["stability"]["expectations"] == 2
    assert r["problems"] == {"degraded": [], "zero_validation": [], "zero_programmatic": []}


def test_programmatic_ratio_none_when_no_assertions():
    e = EvalCase(id=1, prompt="p", expectations=["x"])  # 无 assertion
    r = inspect_evals(_set([e]))
    assert r["stability"]["programmatic_ratio"] is None


def test_flags_degraded_zero_validation_zero_programmatic():
    degraded = EvalCase(id=1, prompt="p", expectations=["e"], assertions=[Assertion(name="a", type=AssertionType.FILE_EXISTS)], flags=["degraded:no-llm"])
    zero_val = EvalCase(id=2, prompt="p")  # 无 assertion 无 expectation
    only_llm = EvalCase(id=3, prompt="p", expectations=["e"], assertions=[Assertion(name="a", type=AssertionType.LLM_FACT)])  # 纯主观
    only_exp = EvalCase(id=4, prompt="p", expectations=["e"])  # 仅 expectation → 零程序化
    r = inspect_evals(_set([degraded, zero_val, only_llm, only_exp]))
    assert r["problems"]["degraded"] == ["eval-1 (id=1)"]
    assert r["problems"]["zero_validation"] == ["eval-2 (id=2)"]
    assert set(r["problems"]["zero_programmatic"]) == {"eval-3 (id=3)", "eval-4 (id=4)"}  # label 带 id 便于定位


def test_problem_label_includes_name_and_id():
    # 有 name 的 eval:problem label = "name (id=N)",兼顾可读 + 精确定位
    e = EvalCase(id=5, name="L1-概念题", prompt="p", expectations=["e"])  # 仅 expectation → 零程序化
    r = inspect_evals(_set([e]))
    assert r["problems"]["zero_programmatic"] == ["L1-概念题 (id=5)"]


def test_empty_set():
    r = inspect_evals(_set([]))
    assert r["completeness"]["evals"] == 0
    assert r["stability"]["programmatic_ratio"] is None
    assert r["problems"] == {"degraded": [], "zero_validation": [], "zero_programmatic": []}
