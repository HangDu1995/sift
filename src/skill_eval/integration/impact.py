"""Impact analyzer — blast radius analysis for new skills."""

from __future__ import annotations

from typing import Any


def analyze_impact(
    new_skill: dict,
    all_skills: list[dict],
    integration_scenarios: list[dict] | None = None,
    skill_keywords: dict[str, list[str]] | None = None,
) -> dict:
    name = new_skill.get("name", "")
    integration_scenarios = integration_scenarios or []
    skill_keywords = skill_keywords or {}

    affected_chains: list[dict] = []
    affected_routing: list[dict] = []

    for scenario in integration_scenarios:
        stype = scenario.get("type", "")
        if stype == "chain":
            chain = scenario.get("chain", [])
            if name in chain:
                affected_chains.append(scenario)
        elif stype == "routing":
            for tp in scenario.get("test_prompts", []):
                triggers = tp.get("should_trigger", [])
                not_triggers = tp.get("should_not_trigger", [])
                if name in triggers or name in not_triggers:
                    affected_routing.append(scenario)
                    break

    routing_tests = []
    if name in skill_keywords:
        routing_tests.append(name)

    return {
        "new_skill": name,
        "affected_skills": [],
        "affected_chains": affected_chains,
        "affected_routing": affected_routing,
        "routing_tests_needed": routing_tests,
    }
