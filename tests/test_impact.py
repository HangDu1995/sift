"""Tests for impact analyzer — blast radius analysis for new skills."""

from __future__ import annotations

import pytest

from skill_eval.integration.impact import analyze_impact


class TestAnalyzeImpact:
    def test_new_skill_no_connections(self):
        result = analyze_impact(
            new_skill={"name": "isolated-skill", "domain": "api", "type": "domain"},
            all_skills=[{"name": "other"}, {"name": "another"}],
            integration_scenarios=[],
            skill_keywords={},
        )
        assert result["new_skill"] == "isolated-skill"
        assert result["affected_skills"] == []
        assert result["affected_chains"] == []

    def test_skill_in_chain(self):
        result = analyze_impact(
            new_skill={"name": "skill-b", "domain": "api", "type": "domain"},
            all_skills=[{"name": "skill-a"}, {"name": "skill-c"}],
            integration_scenarios=[
                {"name": "chain-1", "type": "chain", "chain": ["skill-a", "skill-b", "skill-c"]},
            ],
            skill_keywords={},
        )
        assert len(result["affected_chains"]) == 1
        assert result["affected_chains"][0]["name"] == "chain-1"

    def test_skill_with_keywords(self):
        result = analyze_impact(
            new_skill={"name": "media-player", "domain": "media", "type": "domain"},
            all_skills=[{"name": "video-handler"}],
            integration_scenarios=[],
            skill_keywords={"media-player": ["视频", "播放"]},
        )
        assert "media-player" in result["routing_tests_needed"]

    def test_skill_in_routing_scenario(self):
        result = analyze_impact(
            new_skill={"name": "api-builder", "domain": "api", "type": "domain"},
            all_skills=[{"name": "ui-builder"}],
            integration_scenarios=[
                {"name": "route-1", "type": "routing",
                 "test_prompts": [{"prompt": "build api", "should_trigger": ["api-builder"]}]},
            ],
            skill_keywords={"api-builder": ["api", "build"]},
        )
        assert any(s["name"] == "route-1" for s in result["affected_routing"])
