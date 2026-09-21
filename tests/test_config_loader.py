"""Tests for three-layer config system."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from sift.config.loader import EvalConfig, load_config


@pytest.fixture
def framework_yaml(tmp_path: Path) -> Path:
    pkg_defaults = Path(__file__).parent.parent / "src" / "sift" / "config" / "defaults.yaml"
    if pkg_defaults.exists():
        return pkg_defaults
    return tmp_path / "defaults.yaml"


class TestEvalConfigDefaults:
    @staticmethod
    def test_default_values():
        config = EvalConfig()
        assert config.collection_prefix == ""
        assert config.skills_dir == "skills/"
        assert config.domain_templates == {}
        assert config.type_templates == {}
        assert math.isclose(config.grading_threshold, 0.7, rel_tol=0.0, abs_tol=0.0)

    @staticmethod
    def test_from_dict():
        data = {
            "scanning": {"collection_prefix": "mylib-*"},
            "domain_templates": {
                "api": {
                    "assertions": [
                        {"type": "CONTAINS_PATTERN", "pattern": "router", "description": "has router"},
                    ],
                },
            },
        }
        config = EvalConfig.from_dict(data)
        assert config.collection_prefix == "mylib-*"
        assert "api" in config.domain_templates
        assert len(config.domain_templates["api"]) == 1

    @staticmethod
    def test_from_empty_dict():
        config = EvalConfig.from_dict({})
        assert config.collection_prefix == ""

    @staticmethod
    def test_from_dict_with_skill_keywords():
        data = {
            "skill_keywords": {
                "builder": ["build", "compile"],
                "tester": ["test", "verify"],
            },
        }
        config = EvalConfig.from_dict(data)
        assert "builder" in config.skill_keywords
        assert config.skill_keywords["builder"] == ["build", "compile"]


class TestThreeLayerMerge:
    @staticmethod
    def test_framework_only(tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(
            yaml.dump(
                {
                    "scanning": {"collection_prefix": ""},
                    "grading": {"default_threshold": 0.8},
                }
            )
        )
        config = load_config(tmp_path, config_path=fw)
        assert math.isclose(config.grading_threshold, 0.8, rel_tol=0.0, abs_tol=0.0)

    @staticmethod
    def test_project_overrides_framework(tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(
            yaml.dump(
                {
                    "scanning": {"collection_prefix": ""},
                    "grading": {"default_threshold": 0.5},
                }
            )
        )
        proj = tmp_path / "eval-config.yaml"
        proj.write_text(
            yaml.dump(
                {
                    "scanning": {"collection_prefix": "mylib-*"},
                    "grading": {"default_threshold": 0.9},
                }
            )
        )
        config = load_config(tmp_path, config_path=fw, project_config_path=proj)
        assert config.collection_prefix == "mylib-*"
        assert math.isclose(config.grading_threshold, 0.9, rel_tol=0.0, abs_tol=0.0)

    @staticmethod
    def test_skill_local_overrides_project(tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(yaml.dump({"scanning": {"collection_prefix": ""}}))
        proj = tmp_path / "eval-config.yaml"
        proj.write_text(
            yaml.dump(
                {
                    "domain_templates": {
                        "api": {"assertions": [{"type": "CONTAINS_PATTERN", "pattern": "v1"}]},
                    },
                }
            )
        )
        skill_cfg = tmp_path / "sift.yaml"
        skill_cfg.write_text(
            yaml.dump(
                {
                    "prompt_templates": ["custom prompt 1"],
                }
            )
        )
        config = load_config(tmp_path, config_path=fw, project_config_path=proj, skill_config_path=skill_cfg)
        assert "api" in config.domain_templates
        assert config.prompt_templates == ["custom prompt 1"]


class TestTemplateFlattening:
    @staticmethod
    def test_flatten_nested_assertions():
        data = {
            "domain_templates": {
                "api": {
                    "assertions": [
                        {"type": "CONTAINS_PATTERN", "pattern": "router"},
                        {"type": "NOT_CONTAINS", "pattern": "TODO"},
                    ],
                },
            },
        }
        config = EvalConfig.from_dict(data)
        assert len(config.domain_templates["api"]) == 2

    @staticmethod
    def test_flat_list_assertions():
        data = {
            "domain_templates": {
                "api": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router"},
                ],
            },
        }
        config = EvalConfig.from_dict(data)
        assert len(config.domain_templates["api"]) == 1

    @staticmethod
    def test_missing_yaml_returns_defaults(tmp_path: Path):
        config = load_config(tmp_path, config_path=tmp_path / "nonexistent.yaml")
        assert config.collection_prefix == ""
