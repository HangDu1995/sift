
"""Pure parsing and position-label translation for scoring results."""

from __future__ import annotations

import json
import logging

from sift.models import AnalysisResult, ComparisonExpectationResults, ComparisonResult


def _recompute_expectation_counts(
    er: dict[str, ComparisonExpectationResults] | None,
) -> dict[str, ComparisonExpectationResults] | None:
    """expectation_results 的 passed/total/pass_rate 常与它逐条 details 不一致——comparator
    LLM 一次性把汇总数和逐条列表都吐在同一份 JSON 里,数不对自己的列表(同 rubric 4/5/4→4.67
    那类自报不精确)。details 是逐条 ground truth:有它就据此重算汇总数,覆盖 LLM 声明值,使
    viewer 顶部计数永远跟下方逐条列表对得上;无 details(可选字段没给)仍信任声明值。
    """
    if not er:
        return er
    out: dict[str, ComparisonExpectationResults] = {}
    for side, results in er.items():
        details = results.details or []
        if details:
            passed = sum(1 for d in details if d.passed)
            total = len(details)
            out[side] = results.model_copy(
                update={
                    "passed": passed,
                    "total": total,
                    "pass_rate": passed / total if total else 0.0,
                }
            )
        else:
            out[side] = results
    return out


def parse_comparison_result(response: str) -> ComparisonResult:
    if not response:
        return ComparisonResult(winner="TIE", reasoning="对比调用失败", failed=True)
    try:
        data = json.loads(response)
        return ComparisonResult.model_validate(data)
    except Exception:
        import re

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return ComparisonResult.model_validate(json.loads(match.group()))
            except Exception:
                logging.getLogger(__name__).debug(
                    ("Optional operation failed; continuing with fallback"), exc_info=True
                )
        return ComparisonResult(winner="TIE", reasoning="无法解析对比响应", failed=True)


def translate_comparison_result(
    raw: ComparisonResult, side0_is_pos_a: bool, label_a: str = "with", label_b: str = "without"
) -> ComparisonResult:
    """把 LLM 的 position 标签(A/B/TIE)翻成语义 label。

    - position A 的 material = side0(if side0_is_pos_a)else side1
    - winner: A→pos_a_label(=label_a 若 side0 在 A)否则 label_b;TIE/异常→tie
    - rubric/output_quality/expectation_results: dict key A/B→pos_a_label/pos_b_label
    """
    a_key = label_a if side0_is_pos_a else label_b  # LLM position A → 此 label
    b_key = label_b if side0_is_pos_a else label_a

    def _remap(d: dict | None) -> dict | None:
        if not d:
            return d
        out: dict = {}
        for k, v in d.items():
            ku = (k or "").strip().upper()
            out[a_key if ku == "A" else b_key if ku == "B" else k] = v
        return out

    w = (raw.winner or "").strip().upper()
    if w == "A":
        winner = a_key
    elif w == "B":
        winner = b_key
    else:  # TIE / 空 / 异常值
        winner = "tie"

    return raw.model_copy(
        update={
            "winner": winner,
            "rubric": _remap(raw.rubric),
            "output_quality": _remap(raw.output_quality),
            "expectation_results": _recompute_expectation_counts(_remap(raw.expectation_results)),
            "pos_a_label": a_key,  # 揭盲:LLM position A/B 各自对应的语义 label,供 viewer 标注
            "pos_b_label": b_key,
        }
    )


def parse_analysis_notes(response: str) -> list[str]:
    if not response:
        return []
    try:
        data = json.loads(response)
        if isinstance(data, list):
            return [str(item) for item in data]
    except json.JSONDecodeError:
        notes = _parse_embedded_notes(response)
        if notes is not None:
            return notes
    return [line.strip().lstrip("- ") for line in response.splitlines() if line.strip() and not line.startswith("#")]


def parse_analysis_result(response: str) -> AnalysisResult | None:
    if not response:
        return None
    try:
        data = json.loads(response)
        return AnalysisResult.model_validate(data)
    except Exception:
        import re

        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            try:
                return AnalysisResult.model_validate(json.loads(match.group()))
            except Exception:
                logging.getLogger(__name__).debug(
                    ("Optional operation failed; continuing with fallback"), exc_info=True
                )
        return None


def _parse_embedded_notes(response):
    import re

    match = re.search(r"\[.*\]", response, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return [str(item) for item in data]
        except Exception:
            logging.getLogger(__name__).debug(("Optional operation failed; continuing with fallback"), exc_info=True)
    return None
