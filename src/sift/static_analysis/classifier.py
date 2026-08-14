"""Skill classifier — determines CAPABILITY_UPLIFT vs ENCODED_PREFERENCE."""

from __future__ import annotations

from sift.models import SkillCategory


_PREFERENCE_SIGNALS = [
    # 英文
    "exact template",
    "team convention",
    "company standard",
    "project style",
    "specific format",
    "coding standard",
    "style guide",
    "preferred pattern",
    # 中文(对齐英文语义;"规范"/"风格"作根词,不另列其短语特例,避免子串重叠双计)
    "模板",
    "约定",
    "风格",
    "规范",
    "公司标准",
]

_UPGRADE_SIGNALS = [
    # 英文
    "step-by-step",
    "workflow",
    "best practice",
    "algorithm",
    "technique",
    "methodology",
    "enhanced",
    "advanced",
    "comprehensive",
    # 中文(同上);"技术"/"方法"过泛不加(几乎必现,会淹没信号)
    "分步",
    "逐步",
    "工作流",
    "最佳实践",
    "算法",
    "方法论",
    "进阶",
    "高级",
    "增强",
    "全面",
    "综合",
]


def classify_skill(body: str, description: str) -> SkillCategory:
    text = f"{body}\n{description}".lower()

    pref_score = sum(1 for s in _PREFERENCE_SIGNALS if s in text)
    upgrade_score = sum(1 for s in _UPGRADE_SIGNALS if s in text)

    if pref_score > upgrade_score:
        return SkillCategory.ENCODED_PREFERENCE
    return SkillCategory.CAPABILITY_UPLIFT
