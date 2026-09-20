"""审计前的指令整理缓存，保持命中和未命中时的报告内容一致。"""

from collections import Counter
from typing import Callable

from sift.audit import headline_cache, refine_cache
from sift.models import InstructionCheck


def refine_instructions_cached(instructions: list[InstructionCheck], model: str | None, refine: Callable) -> tuple:
    """缓存指令筛选结果，每次按本次输入重新生成筛除原因统计。"""
    instructions_before = instructions
    key = refine_cache.cache_key(instructions, model)

    def compute():
        refined, _warnings = refine(instructions_before)
        return refined, len(instructions_before) - len(refined)

    instructions, dropped_count = refine_cache.get_or_compute(key, compute)
    warnings = []
    if dropped_count > 0:
        refined_ids = {(r.source.value, r.section, r.instruction_text) for r in instructions}
        dropped_src_counts = Counter(
            item.source.value
            for item in instructions_before
            if (item.source.value, item.section, item.instruction_text) not in refined_ids
        )
        parts = ", ".join(f"{src} {count}" for src, count in dropped_src_counts.items())
        warnings.append(f"refine: 滤除 {dropped_count} 条疑似示例/占位伪指令({parts})")
    return instructions, dropped_count, warnings


def apply_headlines_cached(instructions: list[InstructionCheck], model: str | None, generate: Callable) -> None:
    """读取或生成显示标题，按原指令索引写回。"""
    key = headline_cache.cache_key(instructions, model)

    def compute():
        return generate(instructions)

    headlines = headline_cache.get_or_compute(key, compute)
    for index, instruction in enumerate(instructions):
        if index < len(headlines):
            instruction.headline = headlines[index]
