"""Blind Comparator — compare two outputs without knowing which skill produced them."""

from __future__ import annotations

import json
import random
from pathlib import Path

from skill_eval.models import ComparisonResult
from skill_eval.utils import call_claude


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
        output_with: str,
        output_without: str,
        eval_prompt: str,
        expectations: list[str] | None = None,
    ) -> ComparisonResult:
        """盲比较 with/without 两份输出。

        position 随机化:每次随机决定 with 放 LLM 看到的 A 位还是 B 位(消 position
        bias——LLM 可能偏好首位/末位,恒把 with 放 A 会让偏差系统性偏向/偏离 with)。
        LLM 只看到 position A/B(保持盲);_parse 返回 raw A/B 标签;_translate 在边界
        把 winner 和 rubric/output_quality/expectation_results 的 key 翻成语义 with/without,
        使 ComparisonResult 自描述、viewer 无需知道当次映射。
        """
        with_is_a = self._rng.random() < 0.5
        if with_is_a:
            output_a, output_b = output_with, output_without
        else:
            output_a, output_b = output_without, output_with
        prompt = self._build_prompt(output_a, output_b, eval_prompt, expectations)
        response = self._call(prompt)
        raw = self._parse(response)
        return self._translate(raw, with_is_a=with_is_a)

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

    def _translate(self, raw: ComparisonResult, with_is_a: bool) -> ComparisonResult:
        """把 LLM 的 position 标签(A/B/TIE)翻成语义 with/without/tie。

        - winner: A→with(若 with 在 A 位)否则 without;TIE/异常→tie
        - rubric/output_quality/expectation_results: dict key A/B→with/without
        """
        a_key = "with" if with_is_a else "without"
        b_key = "without" if with_is_a else "with"

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
            "expectation_results": _remap(raw.expectation_results),
        })
