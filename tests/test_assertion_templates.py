"""Tests for assertion template library — config-driven, no hardcoded domain/type templates."""

from __future__ import annotations

import pytest

from skill_eval.config.loader import EvalConfig
from skill_eval.scoring.templates import AssertionTemplateLibrary, get_default_assertions


class TestAssertionTemplateLibrary:
    def test_empty_config_returns_empty(self):
        lib = AssertionTemplateLibrary()
        result = lib.get("nonexistent_domain", "nonexistent_type")
        assert result == []

    def test_domain_from_config(self):
        config = EvalConfig(domain_templates={
            "api": [
                {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "has router"},
                {"type": "NOT_CONTAINS", "pattern": "TODO", "description": "no todos"},
            ],
        })
        lib = AssertionTemplateLibrary(config=config)
        result = lib.get("api")
        assert len(result) == 2
        assert result[0]["type"] == "CONTAINS_PATTERN"

    def test_type_from_config(self):
        config = EvalConfig(type_templates={
            "pipeline": [
                {"type": "JSON_HAS_FIELD", "params": {"field": "status"}, "description": "has status"},
            ],
        })
        lib = AssertionTemplateLibrary(config=config)
        result = lib.get(domain=None, skill_type="pipeline")
        assert len(result) == 1

    def test_domain_and_type_merged_no_duplicates(self):
        config = EvalConfig(
            domain_templates={
                "api": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "has router"},
                ],
            },
            type_templates={
                "pipeline": [
                    {"type": "JSON_HAS_FIELD", "params": {"field": "status"}, "description": "has status"},
                ],
            },
        )
        lib = AssertionTemplateLibrary(config=config)
        result = lib.get(domain="api", skill_type="pipeline")
        assert len(result) == 2

    def test_duplicate_removed(self):
        config = EvalConfig(
            domain_templates={
                "api": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "domain version"},
                ],
            },
            type_templates={
                "pipeline": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "type version"},
                ],
            },
        )
        lib = AssertionTemplateLibrary(config=config)
        result = lib.get(domain="api", skill_type="pipeline")
        assert len(result) == 1

    def test_list_domains(self):
        config = EvalConfig(
            domain_templates={"api": [], "ui": []},
            type_templates={"pipeline": []},
        )
        lib = AssertionTemplateLibrary(config=config)
        assert set(lib.list_domains()) == {"api", "ui"}
        assert set(lib.list_types()) == {"pipeline"}


class TestGetDefaultAssertions:
    def test_no_config_returns_empty(self):
        result = get_default_assertions("nonexistent")
        assert result == []

    def test_with_config(self):
        config = EvalConfig(domain_templates={
            "api": [{"type": "CONTAINS_PATTERN", "pattern": "v1", "description": "api v1"}],
        })
        result = get_default_assertions("api", config=config)
        assert len(result) == 1
        assert result[0]["pattern"] == "v1"
