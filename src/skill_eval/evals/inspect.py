"""Eval-set health inspection — static metrics + concern flags, no verdict."""

from __future__ import annotations

from typing import Any

from skill_eval.models import EvalSet
from skill_eval.models.enums import AssertionType

# 程序化(稳)断言类型;LLM_GRADE/LLM_FACT/LLM_NOT_DELEGATE 是语义(漂)。
# expectations(free-text)另算 LLM 判,不进 assertion 占比分母。
PROGRAMMATIC_TYPES = frozenset({
    AssertionType.FILE_EXISTS, AssertionType.FILE_CONTAINS,
    AssertionType.CONTAINS_PATTERN, AssertionType.JSON_HAS_FIELD,
    AssertionType.LINE_COUNT, AssertionType.NOT_CONTAINS,
    AssertionType.CUSTOM_SCRIPT, AssertionType.CROSS_FILE_CONSISTENCY,
    AssertionType.CODE_COMPILES, AssertionType.CUSTOM_PLUGIN,
    AssertionType.TOOL_CALLED, AssertionType.STEP_COUNT,
})


def inspect_evals(eval_set: EvalSet) -> dict[str, Any]:
    """纯静态体检:完整性 + 稳定性 + 问题 flag。不下综合判决(人决定冻不冻)。"""
    evals = eval_set.evals
    total_assertions = sum(len(e.assertions) for e in evals)
    total_expectations = sum(len(e.expectations) for e in evals)
    prog = sum(1 for e in evals for a in e.assertions if a.type in PROGRAMMATIC_TYPES)
    llm_assert = total_assertions - prog

    levels: dict[str, int] = {}
    for e in evals:
        lv = e.level or "?"
        levels[lv] = levels.get(lv, 0) + 1

    def _label(e):
        return f"{e.get_name()} (id={e.id})"  # name + id,problem 定位用

    degraded = [_label(e) for e in evals
                if any(str(f).startswith("degraded:") for f in (e.flags or []))]
    zero_validation = [_label(e) for e in evals if not e.assertions and not e.expectations]
    # 零程序化:有校验(assertion 或 expectation)但无任何程序化断言 → 纯主观
    zero_programmatic = [_label(e) for e in evals
                         if (e.assertions or e.expectations)
                         and not any(a.type in PROGRAMMATIC_TYPES for a in e.assertions)]

    return {
        "skill_name": eval_set.skill_name,
        "completeness": {
            "evals": len(evals),
            "assertions": total_assertions,
            "expectations": total_expectations,
            "levels": levels,
        },
        "stability": {
            "programmatic": prog,
            "llm": llm_assert,
            "programmatic_ratio": (prog / total_assertions) if total_assertions else None,
            "expectations": total_expectations,
        },
        "problems": {
            "degraded": degraded,
            "zero_validation": zero_validation,
            "zero_programmatic": zero_programmatic,
        },
    }
