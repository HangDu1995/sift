"""Tests for three-layer config system."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_eval.config.loader import EvalConfig, load_config


@pytest.fixture
def framework_yaml(tmp_path: Path) -> Path:
    pkg_defaults = Path(__file__).parent.parent / "src" / "skill_eval" / "config" / "defaults.yaml"
    if pkg_defaults.exists():
        return pkg_defaults
    return tmp_path / "defaults.yaml"


class TestEvalConfigDefaults:
    def test_default_values(self):
        config = EvalConfig()
        assert config.collection_prefix == ""
        assert config.skills_dir == "skills/"
        assert config.domain_templates == {}
        assert config.type_templates == {}
        assert config.grading_threshold == 0.7

    def test_from_dict(self):
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

    def test_from_empty_dict(self):
        config = EvalConfig.from_dict({})
        assert config.collection_prefix == ""

    def test_from_dict_with_skill_keywords(self):
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
    def test_framework_only(self, tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(yaml.dump({
            "scanning": {"collection_prefix": ""},
            "grading": {"default_threshold": 0.8},
        }))
        config = load_config(tmp_path, config_path=fw)
        assert config.grading_threshold == 0.8

    def test_project_overrides_framework(self, tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(yaml.dump({
            "scanning": {"collection_prefix": ""},
            "grading": {"default_threshold": 0.5},
        }))
        proj = tmp_path / "eval-config.yaml"
        proj.write_text(yaml.dump({
            "scanning": {"collection_prefix": "mylib-*"},
            "grading": {"default_threshold": 0.9},
        }))
        config = load_config(tmp_path, config_path=fw, project_config_path=proj)
        assert config.collection_prefix == "mylib-*"
        assert config.grading_threshold == 0.9

    def test_skill_local_overrides_project(self, tmp_path: Path):
        fw = tmp_path / "defaults.yaml"
        fw.write_text(yaml.dump({"scanning": {"collection_prefix": ""}}))
        proj = tmp_path / "eval-config.yaml"
        proj.write_text(yaml.dump({
            "domain_templates": {
                "api": {"assertions": [{"type": "CONTAINS_PATTERN", "pattern": "v1"}]},
            },
        }))
        skill_cfg = tmp_path / "skill-eval.yaml"
        skill_cfg.write_text(yaml.dump({
            "prompt_templates": ["custom prompt 1"],
        }))
        config = load_config(tmp_path, config_path=fw, project_config_path=proj, skill_config_path=skill_cfg)
        assert "api" in config.domain_templates
        assert config.prompt_templates == ["custom prompt 1"]


class TestTemplateFlattening:
    def test_flatten_nested_assertions(self):
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

    def test_flat_list_assertions(self):
        data = {
            "domain_templates": {
                "api": [
                    {"type": "CONTAINS_PATTERN", "pattern": "router"},
                ],
            },
        }
        config = EvalConfig.from_dict(data)
        assert len(config.domain_templates["api"]) == 1

    def test_missing_yaml_returns_defaults(self, tmp_path: Path):
        config = load_config(tmp_path, config_path=tmp_path / "nonexistent.yaml")
        assert config.collection_prefix == ""
