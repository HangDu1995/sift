"""Tests for L0 scanning — eval coverage scanning and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from skill_eval.scanning.scanner import scan_evals, validate_evals_json, check_assertion_coverage


def _make_skill(root: Path, collection: str, skill_name: str, frontmatter: dict | None = None,
                evals: dict | None = None) -> Path:
    col_dir = root / collection / "skills" / skill_name
    col_dir.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        fm_str = yaml.dump(frontmatter, allow_unicode=True)
        content = f"---\n{fm_str}---\n\n# Body\n"
    else:
        content = "# No frontmatter\n"
    (col_dir / "SKILL.md").write_text(content, encoding="utf-8")
    if evals is not None:
        evals_dir = col_dir / "evals"
        evals_dir.mkdir(exist_ok=True)
        (evals_dir / "evals.json").write_text(json.dumps(evals, indent=2), encoding="utf-8")
    return col_dir


def _sample_evals(skill_name: str = "test", count: int = 1) -> dict:
    return {
        "skill_name": skill_name,
        "evals": [
            {"id": i + 1, "prompt": f"prompt {i + 1}", "expected_output": f"expected {i + 1}",
             "files": [], "assertions": [{"name": "check", "type": "FILE_EXISTS", "params": {}}]}
            for i in range(count)
        ],
    }


class TestScanEvals:
    def test_no_skills(self, tmp_path: Path):
        result = scan_evals(tmp_path)
        assert result["total_skills"] == 0

    def test_skill_without_evals(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-skills", "my-skill", {"name": "my-skill"})
        result = scan_evals(tmp_path)
        assert result["total_skills"] == 1
        assert result["skills"][0]["has_evals"] is False

    def test_skill_with_evals(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-skills", "my-skill",
                     {"name": "my-skill"}, evals=_sample_evals("my-skill", 2))
        result = scan_evals(tmp_path)
        assert result["skills"][0]["has_evals"] is True
        assert result["skills"][0]["eval_count"] == 2

    def test_metadata_extracted(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-skills", "api-builder", {
            "name": "api-builder", "type": "domain", "domain": "api", "tags": ["rest"],
        })
        result = scan_evals(tmp_path)
        skill = result["skills"][0]
        assert skill["name"] == "api-builder"
        assert skill["type"] == "domain"
        assert skill["domain"] == "api"

    def test_filters_by_domain(self, tmp_path: Path):
        _make_skill(tmp_path, "mylib-skills", "api-skill", {"name": "api-skill", "domain": "api"})
        _make_skill(tmp_path, "mylib-skills", "ui-skill", {"name": "ui-skill", "domain": "ui"})
        result = scan_evals(tmp_path, filters={"domain": "api"})
        names = [s["name"] for s in result["skills"]]
        assert "api-skill" in names
        assert "ui-skill" not in names

    def test_with_config_prefix(self, tmp_path: Path):
        from skill_eval.config.loader import EvalConfig
        _make_skill(tmp_path, "mylib-core", "skill-a", {"name": "skill-a"})
        _make_skill(tmp_path, "other-lib", "skill-b", {"name": "skill-b"})
        config = EvalConfig(collection_prefix="mylib-*")
        result = scan_evals(tmp_path, config=config)
        names = [s["name"] for s in result["skills"]]
        assert "skill-a" in names
        assert "skill-b" not in names


class TestValidateEvalsJson:
    def test_valid_no_errors(self, tmp_path: Path):
        path = tmp_path / "evals.json"
        path.write_text(json.dumps(_sample_evals()))
        issues = validate_evals_json(path)
        errors = [i for i in issues if i["severity"] == "ERROR"]
        assert errors == []

    def test_missing_skill_name(self, tmp_path: Path):
        data = _sample_evals()
        del data["skill_name"]
        path = tmp_path / "evals.json"
        path.write_text(json.dumps(data))
        issues = validate_evals_json(path)
        assert any("skill_name" in i["message"] for i in issues if i["severity"] == "ERROR")

    def test_empty_prompt(self, tmp_path: Path):
        data = _sample_evals()
        data["evals"][0]["prompt"] = ""
        path = tmp_path / "evals.json"
        path.write_text(json.dumps(data))
        issues = validate_evals_json(path)
        assert any("prompt" in i["message"].lower() for i in issues)

    def test_unknown_assertion_type(self, tmp_path: Path):
        data = _sample_evals()
        data["evals"][0]["assertions"] = [{"name": "bad", "type": "NONEXISTENT", "params": {}}]
        path = tmp_path / "evals.json"
        path.write_text(json.dumps(data))
        issues = validate_evals_json(path)
        assert any("NONEXISTENT" in i["message"] for i in issues if i["severity"] == "ERROR")


class TestCheckAssertionCoverage:
    def test_full_coverage(self):
        data = _sample_evals("x", 2)
        assert check_assertion_coverage(data) == 1.0

    def test_no_assertions(self):
        data = {"skill_name": "x", "evals": [
            {"id": 1, "prompt": "p", "assertions": []},
        ]}
        assert check_assertion_coverage(data) == 0.0

    def test_empty_evals(self):
        assert check_assertion_coverage({"skill_name": "x", "evals": []}) == 0.0
