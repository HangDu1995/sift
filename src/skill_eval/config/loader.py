"""Three-layer configuration loader: framework defaults -> project config -> skill-local."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class EvalConfig:
    def __init__(
        self,
        collection_prefix: str = "",
        skills_dir: str = "skills/",
        domain_templates: dict[str, list[dict]] | None = None,
        type_templates: dict[str, list[dict]] | None = None,
        integration_scenarios: list[dict] | None = None,
        skill_keywords: dict[str, list[str]] | None = None,
        prompt_templates: list[str] | None = None,
        grading_threshold: float = 0.7,
        workspace_defaults: dict | None = None,
        pipelines: list[dict] | None = None,
        extra: dict | None = None,
    ) -> None:
        self.collection_prefix = collection_prefix
        self.skills_dir = skills_dir
        self.domain_templates = domain_templates or {}
        self.type_templates = type_templates or {}
        self.integration_scenarios = integration_scenarios or []
        self.skill_keywords = skill_keywords or {}
        self.prompt_templates = prompt_templates or []
        self.grading_threshold = grading_threshold
        self.workspace_defaults = workspace_defaults or {}
        self.pipelines = pipelines or []
        self.extra = extra or {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalConfig:
        scanning = data.get("scanning", {})
        grading = data.get("grading", {})
        domain_raw = data.get("domain_templates", {})
        type_raw = data.get("type_templates", {})

        return cls(
            collection_prefix=scanning.get("collection_prefix", ""),
            skills_dir=scanning.get("skills_dir", "skills/"),
            domain_templates=_flatten_templates(domain_raw),
            type_templates=_flatten_templates(type_raw),
            integration_scenarios=data.get("integration_scenarios", []),
            skill_keywords=data.get("skill_keywords", {}),
            prompt_templates=data.get("prompt_templates", []),
            grading_threshold=grading.get("default_threshold", 0.7),
            workspace_defaults=data.get("workspace", {}),
            pipelines=data.get("pipelines", []),
            extra={k: v for k, v in data.items() if k not in {
                "scanning", "grading", "domain_templates", "type_templates",
                "integration_scenarios", "skill_keywords", "prompt_templates",
                "workspace", "version", "pipelines",
            }},
        )


def load_config(
    root_dir: Path | str,
    config_path: Path | str | None = None,
    project_config_path: Path | str | None = None,
    skill_config_path: Path | str | None = None,
    skill_dir: Path | str | None = None,
) -> EvalConfig:
    root = Path(root_dir)

    if config_path:
        fw_data = _load_yaml(Path(config_path))
    else:
        bundled = Path(__file__).parent / "defaults.yaml"
        fw_data = _load_yaml(bundled)

    if project_config_path:
        proj_data = _load_yaml(Path(project_config_path))
    else:
        proj_data = _load_yaml(root / "eval-config.yaml")

    if skill_config_path:
        skill_data = _load_yaml(Path(skill_config_path))
    elif skill_dir:
        skill_data = _load_yaml(Path(skill_dir) / "eval-config.yaml")
    else:
        skill_data = {}

    merged = _deep_merge(fw_data, _deep_merge(proj_data, skill_data))
    return EvalConfig.from_dict(merged)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}
    except OSError:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten_templates(raw: dict) -> dict[str, list[dict]]:
    flattened: dict[str, list[dict]] = {}
    for domain, value in raw.items():
        if isinstance(value, list):
            flattened[domain] = value
        elif isinstance(value, dict) and "assertions" in value:
            flattened[domain] = value["assertions"]
        elif isinstance(value, dict):
            flattened[domain] = [value]
    return flattened
