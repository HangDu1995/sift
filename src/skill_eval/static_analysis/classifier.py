"""Skill classifier — determines CAPABILITY_UPLIFT vs ENCODED_PREFERENCE."""

from __future__ import annotations

from skill_eval.models import SkillCategory


_PREFERENCE_SIGNALS = [
    "exact template",
    "team convention",
    "company standard",
    "project style",
    "specific format",
    "coding standard",
    "style guide",
    "preferred pattern",
]

_UPGRADE_SIGNALS = [
    "step-by-step",
    "workflow",
    "best practice",
    "algorithm",
    "technique",
    "methodology",
    "enhanced",
    "advanced",
    "comprehensive",
]


def classify_skill(body: str, description: str) -> SkillCategory:
    text = f"{body}\n{description}".lower()

    pref_score = sum(1 for s in _PREFERENCE_SIGNALS if s in text)
    upgrade_score = sum(1 for s in _UPGRADE_SIGNALS if s in text)

    if pref_score > upgrade_score:
        return SkillCategory.ENCODED_PREFERENCE
    return SkillCategory.CAPABILITY_UPLIFT
