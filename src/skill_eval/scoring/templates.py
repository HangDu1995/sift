"""Assertion template library — config-driven, zero hardcoded domain/type templates.

All domain/type assertion templates come from EvalConfig (loaded from eval-config.yaml).
This module provides the merge/dedup logic but NO default templates.
"""

from __future__ import annotations

from typing import Any

from skill_eval.config.loader import EvalConfig


class AssertionTemplateLibrary:
    def __init__(self, config: EvalConfig | None = None) -> None:
        self._config = config or EvalConfig()

    def get(self, domain: str | None = None, skill_type: str | None = None) -> list[dict]:
        domain_assertions = self._config.domain_templates.get(domain, []) if domain else []
        type_assertions = self._config.type_templates.get(skill_type, []) if skill_type else []
        return _merge_and_deduplicate(domain_assertions, type_assertions)

    def list_domains(self) -> list[str]:
        return sorted(self._config.domain_templates.keys())

    def list_types(self) -> list[str]:
        return sorted(self._config.type_templates.keys())


def get_default_assertions(
    domain: str | None = None,
    skill_type: str | None = None,
    config: EvalConfig | None = None,
) -> list[dict]:
    lib = AssertionTemplateLibrary(config=config)
    return lib.get(domain=domain, skill_type=skill_type)


def _assertion_key(a: dict) -> tuple:
    return (
        a.get("type", ""),
        a.get("pattern", ""),
        a.get("field", ""),
        str(a.get("params", "")),
        str(a.get("filename", "")),
    )


def _merge_and_deduplicate(
    primary: list[dict],
    secondary: list[dict],
) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for a in primary:
        seen[_assertion_key(a)] = a
    for a in secondary:
        key = _assertion_key(a)
        if key not in seen:
            seen[key] = a
    return list(seen.values())
