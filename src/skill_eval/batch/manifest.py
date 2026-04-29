"""Batch manifest loader and dependency discovery."""

from __future__ import annotations

import json
from pathlib import Path

from skill_eval.errors import ManifestError
from skill_eval.models import SkillsManifest


class ManifestLoader:
    def load(self, path: Path) -> SkillsManifest:
        if not path.exists():
            raise ManifestError(f"Manifest not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"Invalid JSON in manifest: {exc}") from exc
        return SkillsManifest.model_validate(data)

    def discover(self, directory: Path) -> SkillsManifest:
        if not directory.is_dir():
            raise ManifestError(f"Not a directory: {directory}")

        skills = []
        for skill_dir in sorted(directory.iterdir()):
            if not skill_dir.is_dir():
                continue
            if (skill_dir / "SKILL.md").exists():
                skills.append({
                    "path": str(skill_dir.relative_to(directory)),
                    "depends_on": [],
                    "disabled": False,
                })

        return SkillsManifest(
            name=directory.name,
            skills=skills,
        )
