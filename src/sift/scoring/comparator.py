"""Blind Comparator — compare two outputs without knowing which skill produced them."""

from __future__ import annotations

import random

from sift.models import ComparisonResult
from sift.scoring.client import ScoringClient
from sift.scoring.prompts import (
    ComparisonInputs,
    build_comparison_prompt,
    load_template,
)
from sift.scoring.results import parse_comparison_result, translate_comparison_result

# comparator 给 LLM 的结构化 schema(forced tool_use 用)。从 ComparisonResult 派生,剔除
# internal 字段(failed/pos_a_label/pos_b_label——由 _parse/_translate 填,LLM 不产),
# LLM 只产 winner/reasoning/rubric/output_quality/expectation_results。$defs 必须保留:
# 嵌套子模型经 $ref 引用,丢了成悬空 $ref(严格后端 400);required=[] 因字段全有默认。
_FULL = ComparisonResult.model_json_schema()
_CMP_PROPS = {k: v for k, v in _FULL["properties"].items() if k not in ("failed", "pos_a_label", "pos_b_label")}
COMPARATOR_SCHEMA: dict = {
    "type": "object",
    "properties": _CMP_PROPS,
    "required": [r for r in _FULL.get("required", []) if r in _CMP_PROPS],
}
if "$defs" in _FULL:
    COMPARATOR_SCHEMA["$defs"] = _FULL["$defs"]


class BlindComparator:
    def __init__(self, model: str | None = None, timeout: int = 120, rng: random.Random | None = None):
        self.model = model
        self.timeout = timeout
        # position 打乱用;默认非确定,测试可注入 seeded/stub rng
        self._rng = rng if rng is not None else random.Random()
        self._prompt_template = load_template("comparator")
        self.client = ScoringClient(model, timeout, COMPARATOR_SCHEMA)

    def compare(self, comparison: ComparisonInputs, *, on_usage=None) -> ComparisonResult:
        """盲比较 side A / side B 两份产出(v1/v2 双 skill:label_a/b=v1/v2;默认 with/without)。

        position 随机化:每次随机决定 side0(output_a)放 LLM 看到的 A 位还是 B 位(消
        position bias——LLM 可能偏好首位/末位,恒定放置会让偏差系统性偏向某侧)。
        LLM 只看到 position A/B(保持盲);_parse 返回 raw A/B 标签;_translate 在边界
        把 winner 和 rubric/output_quality/expectation_results 的 key 翻成语义
        label_a/label_b,使 ComparisonResult 自描述、viewer 无需知道当次映射。
        """
        output_a = comparison.output_a
        output_b = comparison.output_b
        eval_prompt = comparison.eval_prompt
        expectations = comparison.expectations
        label_a = comparison.label_a
        label_b = comparison.label_b
        side0_is_pos_a = self._rng.random() < 0.5
        if side0_is_pos_a:
            pos_a, pos_b = output_a, output_b
        else:
            pos_a, pos_b = output_b, output_a
        prompt = build_comparison_prompt(self._prompt_template, pos_a, pos_b, eval_prompt, expectations)
        response = self.client.complete(prompt, on_usage=on_usage)
        raw = parse_comparison_result(response)
        return translate_comparison_result(raw, side0_is_pos_a=side0_is_pos_a, label_a=label_a, label_b=label_b)
