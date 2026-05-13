"""Tests for overfitting detection: gap calculator, detector, source analyzer."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_eval.models import EvalCase, EvalSet, GeneralizationGap, OverfittingReport, SourceProfile
from skill_eval.overfitting.gap import GapCalculator
from skill_eval.overfitting.detector import OverfittingDetector
from skill_eval.overfitting.source_analyzer import SourceAnalyzer


class TestGapCalculator:
    def test_positive_gap(self):
        calc = GapCalculator()
        domain = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        general = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.6}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.30"},
            }
        }
        gap = calc.calculate(domain, general)
        assert gap.domain_delta == 0.50
        assert gap.general_delta == 0.30
        assert gap.gap == 0.20
        assert gap.verdict == "overfit"

    def test_good_gap(self):
        calc = GapCalculator()
        domain = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        general = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.77}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.47"},
            }
        }
        gap = calc.calculate(domain, general)
        assert gap.verdict == "good"

    def test_warn_gap(self):
        calc = GapCalculator()
        domain = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        general = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.7}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.35"},
            }
        }
        gap = calc.calculate(domain, general)
        assert gap.verdict == "warn"

    def test_empty_delta_string(self):
        calc = GapCalculator()
        domain = {"run_summary": {"delta": {"pass_rate": ""}}}
        general = {"run_summary": {"delta": {"pass_rate": ""}}}
        gap = calc.calculate(domain, general)
        assert gap.gap == 0.0

    def test_negative_gap(self):
        calc = GapCalculator()
        domain = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.8}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.50"},
            }
        }
        general = {
            "run_summary": {
                "with_skill": {"pass_rate": {"mean": 0.9}},
                "without_skill": {"pass_rate": {"mean": 0.3}},
                "delta": {"pass_rate": "+0.60"},
            }
        }
        gap = calc.calculate(domain, general)
        assert gap.gap == pytest.approx(-0.10)
        assert gap.verdict == "good"


class TestOverfittingDetector:
    def test_build_report_no_data(self):
        d = OverfittingDetector()
        report = d.build_report(skill_name="test")
        assert report.overfitting_score == 0.0
        assert report.generalization_gap is None

    def test_build_report_with_gap(self):
        d = OverfittingDetector()
        report = d.build_report(
            skill_name="test",
            gap_data={"verdict": "overfit", "gap": 0.25},
        )
        assert report.overfitting_score >= 0.7
        assert report.generalization_gap.verdict == "overfit"
        assert "generalization gap" in report.recommendation.lower()

    def test_build_report_with_warn(self):
        d = OverfittingDetector()
        report = d.build_report(
            skill_name="test",
            gap_data={"verdict": "warn", "gap": 0.15},
        )
        assert report.overfitting_score == 0.4
        assert report.generalization_gap.verdict == "warn"

    def test_build_report_with_good(self):
        d = OverfittingDetector()
        report = d.build_report(
            skill_name="test",
            gap_data={"verdict": "good", "gap": 0.05},
        )
        assert report.overfitting_score == 0.1
        assert report.generalization_gap.verdict == "good"


class TestOverfittingReportModel:
    def test_default(self):
        r = OverfittingReport(skill_name="test")
        assert r.overfitting_score == 0.0
        assert r.generalization_gap is None

    def test_with_gap(self):
        gap = GeneralizationGap(
            domain_delta=0.5, general_delta=0.3, gap=0.2, verdict="overfit",
        )
        r = OverfittingReport(
            skill_name="test",
            generalization_gap=gap,
            overfitting_score=0.7,
            recommendation="fix it",
        )
        assert r.overfitting_score == 0.7
        assert r.generalization_gap.gap == 0.2


class TestSourceAnalyzer:
    def test_empty_directory(self, tmp_path):
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert profile.project_path == str(tmp_path)
        assert len(profile.sample_files) == 0

    def test_detects_file_types(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        (tmp_path / "config.json").write_text('{"key": "value"}')
        (tmp_path / "readme.md").write_text("# Hello")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert profile.file_type_distribution.get(".py") == 1
        assert profile.file_type_distribution.get(".json") == 1
        assert profile.file_type_distribution.get(".md") == 1

    def test_samples_python_files(self, tmp_path):
        (tmp_path / "main.py").write_text("def hello():\n    return 'world'\n")
        (tmp_path / "utils.py").write_text("import os\n\ndef helper():\n    pass\n")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert len(profile.sample_files) <= 2

    def test_ignores_irrelevant_files(self, tmp_path):
        (tmp_path / "image.png").write_text("fake png")
        (tmp_path / "code.py").write_text("print(1)")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        exts = set(profile.file_type_distribution.keys())
        assert ".png" not in exts

    def test_extracts_keywords(self, tmp_path):
        (tmp_path / "api_handler.py").write_text("def handle():\n    pass\n")
        (tmp_path / "database_config.py").write_text("DB_HOST = 'localhost'\n")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert any("api" in kw or "handler" in kw or "database" in kw or "config" in kw
                   for kw in profile.domain_keywords)

    def test_respects_sample_limit(self, tmp_path):
        for i in range(20):
            (tmp_path / f"file_{i}.py").write_text(f"x = {i}\n")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert len(profile.sample_files) <= 15

    def test_project_summary(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert "Total files:" in profile.project_summary
        assert ".py" in profile.project_summary

    def test_directory_structure(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("print('app')")
        analyzer = SourceAnalyzer()
        profile = analyzer.analyze(tmp_path)
        assert "src" in profile.directory_structure_summary
