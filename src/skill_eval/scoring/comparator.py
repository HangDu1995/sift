"""Blind Comparator — compare two outputs without knowing which skill produced them."""

from __future__ import annotations

import json
import random
from pathlib import Path

from skill_eval.models import ComparisonExpectationResults, ComparisonResult
from skill_eval.utils import call_claude


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
            out[side] = results.model_copy(update={
                "passed": passed,
                "total": total,
                "pass_rate": passed / total if total else 0.0,
            })
        else:
            out[side] = results
    return out


class BlindComparator:
    def __init__(self, model: str | None = None, timeout: int = 120, rng: random.Random | None = None):
        self.model = model
        self.timeout = timeout
        # position 打乱用;默认非确定,测试可注入 seeded/stub rng
        self._rng = rng if rng is not None else random.Random()
        self._prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        prompt_path = Path(__file__).parent.parent.parent.parent / "agents" / "comparator.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return ""

    def compare(
        self,
        output_a: str,
        output_b: str,
        eval_prompt: str,
        expectations: list[str] | None = None,
        label_a: str = "with",
        label_b: str = "without",
    ) -> ComparisonResult:
        """盲比较 side A / side B 两份产出(v1/v2 双 skill:label_a/b=v1/v2;默认 with/without)。

        position 随机化:每次随机决定 side0(output_a)放 LLM 看到的 A 位还是 B 位(消
        position bias——LLM 可能偏好首位/末位,恒定放置会让偏差系统性偏向某侧)。
        LLM 只看到 position A/B(保持盲);_parse 返回 raw A/B 标签;_translate 在边界
        把 winner 和 rubric/output_quality/expectation_results 的 key 翻成语义
        label_a/label_b,使 ComparisonResult 自描述、viewer 无需知道当次映射。
        """
        side0_is_posA = self._rng.random() < 0.5
        if side0_is_posA:
            pos_a, pos_b = output_a, output_b
        else:
            pos_a, pos_b = output_b, output_a
        prompt = self._build_prompt(pos_a, pos_b, eval_prompt, expectations)
        response = self._call(prompt)
        raw = self._parse(response)
        return self._translate(raw, side0_is_posA=side0_is_posA, label_a=label_a, label_b=label_b)

    def _build_prompt(
        self,
        output_a: str,
        output_b: str,
        eval_prompt: str,
        expectations: list[str] | None,
    ) -> str:
        parts = [
            self._prompt_template,
            "",
            "--- COMPARISON TASK ---",
            "",
            f"Eval Prompt: {eval_prompt}",
            "",
            "--- OUTPUT A ---",
            output_a[:8000],
            "",
            "--- OUTPUT B ---",
            output_b[:8000],
        ]
        if expectations:
            parts.append("")
            parts.append("--- EXPECTATIONS ---")
            for e in expectations:
                parts.append(f"- {e}")
        parts.append("")
        parts.append("Write your comparison as JSON matching the comparison schema. All string values (reasoning, strengths, weaknesses, etc.) must be in Chinese (中文).")
        return "\n".join(parts)

    def _call(self, prompt: str) -> str:
        try:
            return call_claude(prompt, model=self.model, timeout=self.timeout, max_turns=1)
        except RuntimeError:
            return ""

    def _parse(self, response: str) -> ComparisonResult:
        if not response:
            return ComparisonResult(winner="TIE", reasoning="对比调用失败", failed=True)
        try:
            data = json.loads(response)
            return ComparisonResult.model_validate(data)
        except (json.JSONDecodeError, Exception):
            import re
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if match:
                try:
                    return ComparisonResult.model_validate(json.loads(match.group()))
                except Exception:
                    pass
            return ComparisonResult(winner="TIE", reasoning="无法解析对比响应", failed=True)

    def _translate(self, raw: ComparisonResult, side0_is_posA: bool, label_a: str = "with", label_b: str = "without") -> ComparisonResult:
        """把 LLM 的 position 标签(A/B/TIE)翻成语义 label。

        - position A 的 material = side0(if side0_is_posA)else side1
        - winner: A→pos_a_label(=label_a 若 side0 在 A)否则 label_b;TIE/异常→tie
        - rubric/output_quality/expectation_results: dict key A/B→pos_a_label/pos_b_label
        """
        a_key = label_a if side0_is_posA else label_b   # LLM position A → 此 label
        b_key = label_b if side0_is_posA else label_a

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

        return raw.model_copy(update={
            "winner": winner,
            "rubric": _remap(raw.rubric),
            "output_quality": _remap(raw.output_quality),
            "expectation_results": _recompute_expectation_counts(_remap(raw.expectation_results)),
            "pos_a_label": a_key,   # 揭盲:LLM position A/B 各自对应的语义 label,供 viewer 标注
            "pos_b_label": b_key,
        })
