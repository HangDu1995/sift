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

    def test_generate_html_grading_verdict_aware(self, tmp_path):
        """grading 主列表 verdict 感知:UNEVAL/TIMEOUT/ERROR 不再被混成红 ✗(与 checkpoints 对齐)。

        RED 驱动:renderGrades 循环变量是 exp(checkpoints 用 e./a.),锚定 exp.verdict/exp.source
        避开 checkpoints 已含的 无法评估/超时/错误 字面量(改前就 true = 假绿)。
        """
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs" / "out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.33, "passed": 1, "failed": 2, "total": 3},
            "expectations": [
                {"text": "uneval one", "passed": False, "verdict": "UNEVAL", "source": "llm"},
                {"text": "error one", "passed": False, "verdict": "ERROR", "source": "programmatic"},
                {"text": "pass one", "passed": True, "verdict": "PASS", "source": "programmatic"},
            ],
        }))

        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        # RED 驱动断言:renderGrades 现引用了 verdict/source(checkpoints 用 e./a. 不污染)
        assert "exp.verdict" in html
        assert "exp.source" in html
        # 数据流断言(文档性):verdict 真嵌入了 EMBEDDED_DATA
        assert '"verdict": "UNEVAL"' in html

    def test_generate_html_calibration_diagnostics(self, tmp_path):
        """calibration 块补 confidence_signals / disagreements / 双判计数展示。

        RED 驱动:renderGrades 的 calibration 块循环变量是 cal(别处没有 cal. 前缀),
        锚定 cal.confidence_signals / cal.disagreements 避免撞别处字面量(改前 grep=0)。
        """
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs" / "out.txt").write_text("hello")
        (ws_dir / "calibration.json").write_text(json.dumps({
            "grade": "LOW",
            "agreement_rate": None,
            "overall_confidence": 0.6,
            "recommendations": ["增加双判样本"],
            "both_evaluated": 1,
            "programmatic_only_count": 0,
            "llm_only_count": 0,
            "confidence_signals": [
                {"signal_type": "GRADER_SKIPPED_CHECKS",
                 "description": "测试信号", "impact": -0.1},
            ],
            "disagreements": [
                {"expectation_text": "disagree one",
                 "programmatic_verdict": False, "llm_verdict": True,
                 "resolution": "TRUST_PROGRAMMATIC",
                 "evidence_analysis": "程序化断言更可靠"},
            ],
        }))

        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        # RED 驱动断言:calibration 块现引用 cal.confidence_signals / cal.disagreements
        assert "cal.confidence_signals" in html
        assert "cal.disagreements" in html
        # 数据流断言(文档性,改前改后都通过):confidence_signals 描述真嵌入了 EMBEDDED_DATA
        assert "测试信号" in html

    def test_generate_html_renders_comparison_labels(self, tmp_path):
        """comparison_summary 的 label_a/b 驱动盲评头条显示(块 B v1/v2 真标签)。"""
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {"skill_name": "myskill"},
            "runs": [],
            "run_summary": {},
            "comparisons": [{"winner": "v1", "reasoning": "ok"}],
            "comparison_summary": {
                "a_wins": 3, "b_wins": 1, "ties": 0, "failed": 0,
                "valid": 4, "win_rate": 0.75, "label_a": "v1", "label_b": "v2",
            },
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # headline 是 JS 运行时构建,静态 HTML 不含渲染产物;验数据流 + 模板逻辑:
        # ① comparison_summary 连同 label_a 嵌入 EMBEDDED_DATA
        assert '"label_a": "v1"' in html
        # ② 模板用 compSummary.label_a 渲染头条(非硬编码)
        assert "compSummary.label_a" in html
        assert "主观侧主位 · With Skill 盲评胜率" not in html  # 硬编码字面量已去

    def test_viewer_run_badge_uses_config_field(self, tmp_path):
        """run-listing config badge 用 run.config 字段(非 run.id 正则)→ v1/v2 也能显示 badge。"""
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {"metadata": {"skill_name": "x"}, "runs": [], "run_summary": {}}
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        assert "run.config" in html                       # 用 config 字段驱动 badge
        assert "run.id.match(/(with_skill" not in html    # 旧 run.id 正则已去(v1/v2 不匹配)

    def test_gate_badge_embedded_when_gate_json_present(self, tmp_path):
        """gate.json 在 iteration 目录 → 嵌入 EMBEDDED_DATA.gate + 模板引用(块 C 徽章)。"""
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        (workspace / "gate.json").write_text(json.dumps({
            "decision": "PASS", "exit_code": 0, "reason": "ok",
            "candidate": "v1", "baseline": "v2",
        }))
        benchmark = {"metadata": {"skill_name": "x"}, "runs": [], "run_summary": {}}
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # ① gate 判决连同 decision 嵌入 EMBEDDED_DATA
        assert '"decision": "PASS"' in html
        assert '"candidate": "v1"' in html
        # ② 模板用 EMBEDDED_DATA.gate 渲染徽章(非硬编码)
        assert "EMBEDDED_DATA.gate" in html
        assert "gate-badge" in html

    def test_no_gate_json_no_crash(self, tmp_path):
        """无 gate.json → 不崩溃,徽章隐藏。"""
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        gen = ReportGenerator()
        html = gen.generate_html(workspace, {"metadata": {"skill_name": "x"}, "runs": [], "run_summary": {}})
        assert "EMBEDDED_DATA.gate" in html  # 模板逻辑仍在

    def test_gate_badge_finds_gate_json_in_iteration_subdir(self, tmp_path):
        """canonical 流:gate.json 落 iteration-1/(gate 默认写 benchmark_path.parent),
        report workspace 是父目录 → 仍能找到(与 _load_vet_report 同款 fallback)。"""
        workspace = tmp_path / "out"           # report 的 workspace(输出目录)
        (workspace / "iteration-1").mkdir(parents=True)
        (workspace / "iteration-1" / "gate.json").write_text(json.dumps({
            "decision": "FAIL", "exit_code": 1, "reason": "regressed",
        }))
        gen = ReportGenerator()
        html = gen.generate_html(workspace, {"metadata": {"skill_name": "x"}, "runs": [], "run_summary": {}})
        assert '"decision": "FAIL"' in html   # 从 iteration-1/gate.json 读到


class TestLoadAuxData:
    def test_load_vet_report(self, tmp_path: Path):
        v = tmp_path / "vet-report.json"
        v.write_text(json.dumps({"verdict": "SAFE"}))
        result = _load_vet_report(tmp_path)
        assert result is not None
        assert result["verdict"] == "SAFE"

    def test_load_vet_report_missing(self, tmp_path: Path):
        assert _load_vet_report(tmp_path) is None
