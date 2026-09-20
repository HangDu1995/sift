"""Eval-set health inspection — static metrics + concern flags, no verdict."""

from __future__ import annotations

from typing import Any

from sift.models import EvalSet
from sift.models.enums import AssertionType

# 程序化(稳)断言类型;LLM_GRADE/LLM_FACT/LLM_NOT_DELEGATE 是语义(漂)。
# expectations(free-text)另算 LLM 判,不进 assertion 占比分母。
PROGRAMMATIC_TYPES = frozenset(
    {
        AssertionType.FILE_EXISTS,
        AssertionType.FILE_CONTAINS,
        AssertionType.CONTAINS_PATTERN,
        AssertionType.JSON_HAS_FIELD,
        AssertionType.LINE_COUNT,
        AssertionType.NOT_CONTAINS,
        AssertionType.CUSTOM_SCRIPT,
        AssertionType.CROSS_FILE_CONSISTENCY,
        AssertionType.CODE_COMPILES,
        AssertionType.CUSTOM_PLUGIN,
        AssertionType.TOOL_CALLED,
        AssertionType.STEP_COUNT,
    }
)


def same_prompt_groups(eval_set: EvalSet) -> list[dict[str, Any]]:
    """同 prompt(>1 道)的题组——给读者一个「这是多 scenario、非重复」的提示。

    `--from-transcript` 出卷常产同 prompt 多道(同一用户指令、不同输入 fixture);
    生成器判定 fixture 冲突时不合并、各道带 ``split:N-evals-conflict`` flag
    (见 generator `_merge_same_task_evals`)。人肉翻 evals.json 见 N 个一模一样的
    prompt 容易误读成「重复 bug」,本函数把这组显式列出来堵这个误读。

    每组:``{prompt, count, ids, split_flag}``。``split_flag`` = 组内任一道带的
    ``split:*-evals-conflict`` flag(没有则 None,如手写卷或未过合并门)。
    按 count 降序、再按 prompt 排序,稳态可读。
    """
    groups: dict[str, list] = {}
    for e in eval_set.evals:
        groups.setdefault(e.prompt, []).append(e)
    out: list[dict[str, Any]] = []
    for prompt, members in groups.items():
        if len(members) < 2:
            continue
        split = _find_split_flag(members)
        out.append(
            {
                "prompt": prompt,
                "count": len(members),
                "ids": [e.id for e in members],
                "split_flag": split,
            }
        )
    out.sort(key=lambda g: (-g["count"], g["prompt"]))
    return out


def inspect_evals(eval_set: EvalSet) -> dict[str, Any]:
    """纯静态体检:完整性 + 稳定性 + 问题 flag。不下综合判决(人决定冻不冻)。"""
    evals = eval_set.evals
    total_assertions = sum(len(e.assertions) for e in evals)
    total_expectations = sum(len(e.expectations) for e in evals)
    prog = _count_programmatic_assertions(evals)
    llm_assert = total_assertions - prog

    levels: dict[str, int] = {}
    for e in evals:
        lv = e.level or "?"
        levels[lv] = levels.get(lv, 0) + 1

    def _label(e):
        return f"{e.get_name()} (id={e.id})"  # name + id,problem 定位用

    degraded = [_label(e) for e in evals if any(str(f).startswith("degraded:") for f in (e.flags or []))]
    zero_validation = [_label(e) for e in evals if not e.assertions and not e.expectations]
    # 零程序化:有校验(assertion 或 expectation)但无任何程序化断言 → 纯主观
    zero_programmatic = [_label(e) for e in evals if _has_only_subjective_validation(e)]

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
        "same_prompt_groups": same_prompt_groups(eval_set),
    }


def _find_split_flag(members) -> str | None:
    """Return the first explicit split flag in member and flag order."""
    for member in members:
        for flag in member.flags or []:
            text = str(flag)
            if text.startswith("split:") and text.endswith("-evals-conflict"):
                return text
    return None


def _count_programmatic_assertions(evals) -> int:
    """Count programmatic assertions across all cases."""
    count = 0
    for eval_case in evals:
        count += sum(assertion.type in PROGRAMMATIC_TYPES for assertion in eval_case.assertions)
    return count


def _has_only_subjective_validation(eval_case) -> bool:
    """Distinguish subjective-only cases from cases without validation."""
    if not eval_case.assertions and not eval_case.expectations:
        return False
    return not any(assertion.type in PROGRAMMATIC_TYPES for assertion in eval_case.assertions)
