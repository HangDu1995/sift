"""Tests for reporting/viewer module."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skill_eval.reporting.viewer import (
    ReportGenerator,
    _build_run,
    _load_vet_report,
    find_runs,
)


class TestBuildRunConfigExtraction:
    """Verify _build_run correctly extracts config from nested dir structures."""

    def build_run_dir(self, root: Path, rel_path: str, with_grading: bool = True) -> Path:
        run_dir = root / rel_path
        run_dir.mkdir(parents=True)
        outputs_dir = run_dir / "outputs"
        outputs_dir.mkdir()
        (outputs_dir / "result.txt").write_text("test output")
        if with_grading:
            (run_dir / "grading.json").write_text(json.dumps({
                "summary": {"pass_rate": 0.8, "passed": 4, "failed": 1, "total": 5},
                "expectations": [],
            }))
        return run_dir

    def test_flat_config_with_skill(self, tmp_path: Path):
        run_dir = self.build_run_dir(tmp_path, "with_skill/run-1")
        result = _build_run(tmp_path, run_dir)
        assert result is not None
        assert result["config"] == "with_skill"
        assert result["run_number"] == 1

    def test_flat_config_without_skill(self, tmp_path: Path):
        run_dir = self.build_run_dir(tmp_path, "without_skill/run-3")
        result = _build_run(tmp_path, run_dir)
        assert result is not None
        assert result["config"] == "without_skill"
        assert result["run_number"] == 3

    def test_nested_eval_config(self, tmp_path: Path):
        run_dir = self.build_run_dir(tmp_path, "with_skill/eval-5/run-2")
        result = _build_run(tmp_path, run_dir)
        assert result is not None
        assert result["config"] == "with_skill"
        assert result["eval_id"] == 5
        assert result["run_number"] == 2

    def test_nested_eval_config_wos(self, tmp_path: Path):
        run_dir = self.build_run_dir(tmp_path, "without_skill/eval-10/run-3")
        result = _build_run(tmp_path, run_dir)
        assert result is not None
        assert result["config"] == "without_skill"
        assert result["eval_id"] == 10
        assert result["run_number"] == 3

    def test_nested_with_retry(self, tmp_path: Path):
        run_dir = self.build_run_dir(tmp_path, "with_skill/eval-3/run-1-retry-2")
        result = _build_run(tmp_path, run_dir)
        assert result is not None
        assert result["config"] == "with_skill"
        assert result["eval_id"] == 3
        assert result["run_number"] == 1

    def test_outputs_missing_skips(self, tmp_path: Path):
        run_dir = tmp_path / "with_skill/run-1"
        run_dir.mkdir(parents=True)
        result = _build_run(tmp_path, run_dir)
        assert result is None


class TestFindRuns:
    def test_finds_nested_runs(self, tmp_path: Path):
        for i in range(1, 4):
            d = tmp_path / f"with_skill/eval-{i}/run-1"
            d.mkdir(parents=True)
            (d / "outputs").mkdir()
            (d / "outputs/file.txt").write_text("data")
            (d / "grading.json").write_text(json.dumps({
                "summary": {"pass_rate": 0.5, "passed": 1, "failed": 1, "total": 2},
                "expectations": [],
            }))

        runs = find_runs(tmp_path)
        assert len(runs) == 3
        configs = {r["config"] for r in runs}
        assert configs == {"with_skill"}

    def test_skips_hidden_dirs(self, tmp_path: Path):
        d = tmp_path / ".git/something/run-1"
        d.mkdir(parents=True)
        (d / "outputs").mkdir()
        runs = find_runs(tmp_path)
        assert len(runs) == 0


class TestReportGeneratorStatic:
    def test_generate_html_writes_static(self, tmp_path: Path):
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs/out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "expectations": [{"text": "works", "passed": True}],
        }))

        gen = ReportGenerator()
        output = tmp_path / "report.html"
        gen.write_static(workspace, output)

        assert output.exists()
        html = output.read_text()
        assert "评测回顾" in html or "EMBEDDED_DATA" in html

    def test_generate_html_with_benchmark(self, tmp_path: Path):
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs/result.txt").write_text("ok")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 1, "failed": 0, "total": 1},
            "expectations": [],
        }))

        benchmark = {
            "metadata": {"skill_name": "test-skill"},
            "runs": [],
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 1.0}},
                "without_skill": {"pass_rate": {"mean": 0.5}},
                "delta": {"pass_rate": "+0.50"},
            },
        }

        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        assert "test-skill" in html
        assert "benchmark" in html.lower()


class TestLoadAuxData:
    def test_load_vet_report(self, tmp_path: Path):
        v = tmp_path / "vet-report.json"
        v.write_text(json.dumps({"verdict": "SAFE"}))
        result = _load_vet_report(tmp_path)
        assert result is not None
        assert result["verdict"] == "SAFE"

    def test_load_vet_report_missing(self, tmp_path: Path):
        assert _load_vet_report(tmp_path) is None
