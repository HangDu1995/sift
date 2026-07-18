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

    def test_dedup_retry_dirs_keeps_final(self, tmp_path: Path):
        """runner 在 UNEVAL/失败时自动重试,产出 run-N + run-N-retry-K。
        viewer 只应显示最终 retry(canonical),被取代的失败旧尝试当独立 run 显示会误导。
        与 benchmark/comparison 的 index_runs 同口径。"""
        base = tmp_path / "with_skill/eval-1"
        for name, passed in (("run-1", 0), ("run-1-retry-1", 0), ("run-1-retry-2", 3)):
            d = base / name
            d.mkdir(parents=True)
            (d / "outputs").mkdir()
            (d / "outputs/file.txt").write_text("data")
            (d / "grading.json").write_text(json.dumps({
                "summary": {"pass_rate": passed / 3, "passed": passed, "failed": 3 - passed, "total": 3},
                "expectations": [],
            }))

        runs = find_runs(tmp_path)
        # 只保留最终 retry(run-1-retry-2),不是 3 个
        assert len(runs) == 1
        assert runs[0]["id"].endswith("run-1-retry-2")
        assert runs[0]["grading"]["summary"]["passed"] == 3

    def test_dedup_retry_no_retry_keeps_base(self, tmp_path: Path):
        """无 retry 的普通 run 不受去重影响。"""
        for i in (1, 2):
            d = tmp_path / f"with_skill/eval-{i}/run-1"
            d.mkdir(parents=True)
            (d / "outputs").mkdir()
            (d / "outputs/file.txt").write_text("data")
        runs = find_runs(tmp_path)
        assert len(runs) == 2


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

    def test_generate_html_calibration_block_new_schema(self, tmp_path):
        """校准块改成 verdict 判决;旧 confidence_signals/disagreements/等级 字段移除。

        RED 驱动:锚定 V[cal.verdict](新模板独有查表渲染),且旧 cal.confidence_signals
        不再出现在模板里。
        """
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs" / "out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.85, "passed": 17, "failed": 3, "total": 20},
            "expectations": [],
        }))
        (ws_dir / "calibration.json").write_text(json.dumps({
            "verdict": "HIGH_SCORE_SUSPECT",
            "result_pass": True,
            "process_status": "violated",
            "hard_violations": [{"category": "prohibition",
                                 "instruction_text": "禁止编造错误码",
                                 "evidence": "E-FAKE-999 编造"}],
            "soft_notes": {"step": 2},
            "scoring_rate": 0.85,
            "compliance_rate": 0.5,
        }))

        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        # RED 驱动:新模板用 V[cal.verdict] 查表渲染 verdict 标签
        assert "V[cal.verdict]" in html
        assert "高分存疑" in html          # verdict 标签真渲染
        assert "E-FAKE-999" in html       # 硬违规 evidence 真嵌入 EMBEDDED_DATA
        # 旧凑数字段已从模板移除
        assert "cal.confidence_signals" not in html
        assert "cal.disagreements" not in html
        assert "校准等级" not in html

    def test_generate_html_compliance_per_run_and_aggregate(self, tmp_path):
        """遵循性 per-run(评估详情里 cr.summary)+ 整个考试聚合(基准测试 tab)并存:
        report 既含 per-run 渲染、也含聚合(compliance_aggregate);findings 文本两处都可见。"""
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs" / "out.txt").write_text("hello")
        (ws_dir / "compliance.json").write_text(json.dumps({
            "skill_name": "myskill",
            "transcripts": ["transcript.jsonl"],
            "summary": {"total": 1, "pass": 1, "fail": 0, "na": 0, "unclear": 0},
            "findings": [
                {"instruction_text": "禁止编造错误码", "source": "CONSTRAINT",
                 "verdict": "PASS", "category": "prohibition", "evidence": "未发现编造",
                 "confidence": 0.9, "transcript": "transcript.jsonl"},
            ],
        }))

        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        # 聚合渲染块存在(基准测试 tab)
        assert "renderComplianceAggregate" in html
        assert "compliance_aggregate" in html
        # per-run 遵循性块也渲染(评估详情里 cr.summary)
        assert "cr.summary" in html
        # findings 文本 per-run 与聚合两处都可见
        assert "禁止编造错误码" in html

    def test_generate_html_has_run_jump_select(self, tmp_path):
        """单次执行 tab 支持跳转到指定 run(不必逐个翻 上一个/下一个)。

        RED 驱动:header 加 <select id="run-jump">,JS buildRunSelect 从 EMBEDDED_DATA.runs
        按 eval 分组(<optgroup>)填充、change → showRun 跳转。断言 id + JS 入口存在
        (纯 JS 交互行为靠 node --check 语法 + 重生成 report 肉眼验证,pytest render-smoke 不执行 JS)。
        """
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs/out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 1, "failed": 0, "total": 1},
            "expectations": [{"text": "ok", "passed": True}],
        }))

        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        assert 'id="run-jump"' in html      # 跳转 select 存在
        assert "buildRunSelect" in html      # 填充入口 JS 存在
        assert "optgroup" in html            # 按 eval 分组

    def test_generate_html_grading_summary_shows_per_source_split(self, tmp_path):
        """打分 summary 行显 programmatic/llm per-source 计数——数据早有(ab5ae58),
        viewer 此前只显合并 pass_rate。RED 锚定 summary.passed_programmatic / total_llm。"""
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs/out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.76, "passed": 22, "failed": 7, "total": 29,
                        "passed_programmatic": 10, "total_programmatic": 14,
                        "passed_llm": 12, "total_llm": 15},
            "expectations": [{"text": "ok", "passed": True}],
        }))
        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        assert "summary.passed_programmatic" in html   # 打分块引用 per-source 字段
        assert "summary.total_llm" in html

    def test_generate_html_compliance_confidence_label_not_number(self, tmp_path):
        """遵循性块 confidence 显分类标签(LLM 判定/判不准),不显裸数字——对齐
        audit-report.html confTag(数字只进 title)。RED:'LLM 判定' 在、'置信 ' 不在。"""
        workspace = tmp_path / "iteration-1"
        ws_dir = workspace / "with_skill" / "run-1"
        ws_dir.mkdir(parents=True)
        (ws_dir / "outputs").mkdir()
        (ws_dir / "outputs/out.txt").write_text("hello")
        (ws_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 1, "failed": 0, "total": 1},
            "expectations": [{"text": "ok", "passed": True}],
        }))
        gen = ReportGenerator()
        html = gen.generate_html(workspace, None)
        assert "LLM 判定" in html        # 分类标签口径同 audit-report.html
        assert "置信 " not in html       # 旧「置信 0.80」裸数字已去

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

    def test_generate_html_comparison_quality_shows_all_strengths_weaknesses(self, tmp_path):
        """A: comparison 的 strengths/weaknesses 上游是 list[str] 可多项,viewer 旧实现只显首项 [0] → 显全部。

        RED 锚点(负向):改前模板含 q.strengths[0]/q.weaknesses[0](只取首项)→
        断言 "not in" 失败 = RED;改后移除 [0] 改循环 → GREEN。
        """
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {"skill_name": "x"},
            "runs": [],
            "run_summary": {},
            "comparisons": [{
                "winner": "with",
                "reasoning": "ok",
                "output_quality": {
                    "with": {"score": 8, "strengths": ["优点一", "优点二"],
                             "weaknesses": ["缺点一", "缺点二"]},
                    "without": {"score": 5, "strengths": ["w优点"], "weaknesses": ["w缺点"]},
                },
            }],
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # RED 锚点:不再只取首项 [0]
        assert "q.strengths[0]" not in html
        assert "q.weaknesses[0]" not in html
        # 数据流(文档性,改前改后都通过):多项值真嵌入 EMBEDDED_DATA
        assert "优点二" in html and "缺点二" in html

    def test_generate_html_analysis_renders_transcript_insights(self, tmp_path):
        """B: analysis.transcript_insights (dict[str,str]) 旧实现完全没显 → 补一段展示。

        RED 锚点:改前模板不引用 analysis.transcript_insights(改前 grep=0)→
        断言 "in" 失败 = RED;改后 GREEN。
        """
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {"skill_name": "x"},
            "runs": [],
            "run_summary": {},
            "analysis": {
                "winner_strengths": ["胜者优势"],
                "transcript_insights": {
                    "winner_execution_pattern": "探索工作目录 → 读取 error-codes.md",
                },
            },
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # RED 锚点:模板引用 analysis.transcript_insights
        assert "analysis.transcript_insights" in html
        # 数据流(文档性):insights 真嵌入 EMBEDDED_DATA
        assert "winner_execution_pattern" in html

    def test_generate_html_comparison_renders_expectation_results(self, tmp_path):
        """C: comparison.expectation_results (dict[label→{passed,total,pass_rate,details}]) 旧只 normalize 不渲染 → 补展示。

        RED 锚点:改前 normalize 循环用的是 comp[_f] 方括号,点号 comp.expectation_results 改前 grep=0 →
        断言 "in" 失败 = RED;改后(渲染写 comp.expectation_results[la])GREEN。
        """
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {"skill_name": "x"},
            "runs": [],
            "run_summary": {},
            "comparisons": [{
                "winner": "with",
                "reasoning": "ok",
                "expectation_results": {
                    "with": {"passed": 2, "total": 4, "pass_rate": 0.5,
                             "details": [{"text": "exp1", "passed": True},
                                         {"text": "exp2", "passed": False}]},
                    "without": {"passed": 0, "total": 4, "pass_rate": 0.0,
                                "details": [{"text": "exp1", "passed": False}]},
                },
            }],
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # RED 锚点:模板用点号引用 comp.expectation_results
        assert "comp.expectation_results" in html
        # 数据流(文档性):details 文本真嵌入 EMBEDDED_DATA
        assert "exp1" in html and "exp2" in html

    def test_generate_html_metadata_renders_models(self, tmp_path):
        """D: metadata 的 comparator/analyzer/executor model 字段(str,可能空)旧没显 → 显非空项。

        RED 锚点:改前模板不引用 metadata.comparator_model(改前 grep=0)→
        断言 "in" 失败 = RED;改后 GREEN。
        """
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {
                "skill_name": "x",
                "executor_model": "",
                "analyzer_model": "default",
                "comparator_model": "default",
            },
            "runs": [],
            "run_summary": {},
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # RED 锚点:模板引用 metadata.comparator_model
        assert "metadata.comparator_model" in html

    def test_generate_html_anomaly_header_shows_denominator(self, tmp_path):
        """anomaly 标题加分母 total_runs:"X 个异常" → "X 个异常 / 共 Y 次"(一眼判严重度)。

        total_runs 有真实 writer(benchmark.py:252 = len(runs)),非死字段(对照梯队二①
        retry_count 死字段教训——恒 0 不渲染,这里恒有值才渲染)。每条 detail(含 value/
        threshold)L1298 已显;unresolved_count 与 anomaly_count 恒等(benchmark.py:249/253)
        冗余不显。真缺口仅报告级分母。

        RED 锚点:改前标题只拼 anomaly_count,无 total_runs → 断言 "共 20 次" 失败 = RED;
        改后 GREEN。
        """
        workspace = tmp_path / "iteration-1"
        workspace.mkdir()
        benchmark = {
            "metadata": {"skill_name": "x"},
            "runs": [],
            "run_summary": {},
            "anomaly_report": {
                "anomalies": [{
                    "eval_id": 1, "eval_name": "e", "configuration": "with",
                    "run_number": 1, "anomaly_type": "outlier",
                    "detail": "Duration 123s > median 45s + 3×MAD 12s",
                    "value": 123.0, "threshold": 57.0,
                }],
                "total_runs": 20,
                "anomaly_count": 1,
                "retried_count": 0,
                "unresolved_count": 1,
            },
        }
        gen = ReportGenerator()
        html = gen.generate_html(workspace, benchmark)
        # RED 锚点:模板引用 anomalyReport.total_runs(改前只拼 anomaly_count,grep=0 → RED;
        # 改后 GREEN)。注:viewer 是 client-side JS,"共 20 次" 是运行时拼接的连续串,
        # pytest 读静态 HTML(JS 不执行)验不了——只能验"模板是否引用该字段"(JS 源码表达式
        # 子串),同 metadata.comparator_model 锚点模式。运行时正确性靠 node --check + 人审。
        assert "anomalyReport.total_runs" in html
        # 数据流(文档性,改前改后都过):detail 嵌入 EMBEDDED_DATA
        assert "Duration 123s" in html


class TestLoadAuxData:
    def test_load_vet_report(self, tmp_path: Path):
        v = tmp_path / "vet-report.json"
        v.write_text(json.dumps({"verdict": "SAFE"}))
        result = _load_vet_report(tmp_path)
        assert result is not None
        assert result["verdict"] == "SAFE"

    def test_load_vet_report_missing(self, tmp_path: Path):
        assert _load_vet_report(tmp_path) is None


class TestGenerateReportHtml:
    """generate_report_html:workspace → report.html 核心(run 末尾 + report 命令共用)。
    monkeypatch ReportGenerator.write_static 隔离渲染,只验 benchmark/scorecard 加载逻辑。"""

    def test_loads_benchmark_and_scorecard(self, tmp_path: Path):
        from skill_eval.cli.commands.report import generate_report_html

        (tmp_path / "benchmark.json").write_text(
            json.dumps({"skill_name": "s", "runs": []}), encoding="utf-8")
        (tmp_path / "scorecard.json").write_text(
            json.dumps({"score": 7, "max_score": 11}), encoding="utf-8")

        captured = {}

        def fake_write_static(self, workspace, output_path, benchmark=None, previous=None, scorecard=None):  # noqa: ARG002
            captured.update(benchmark=benchmark, previous=previous,
                             scorecard=scorecard, output_path=output_path)
            output_path.write_text("<html></html>", encoding="utf-8")

        with patch.object(ReportGenerator, "write_static", fake_write_static):
            out = generate_report_html(tmp_path)

        assert out == tmp_path / "report.html"
        assert captured["benchmark"] == {"skill_name": "s", "runs": []}
        assert captured["scorecard"] == {"score": 7, "max_score": 11}
        assert captured["previous"] is None
        assert captured["output_path"] == tmp_path / "report.html"

    def test_no_benchmark_returns_none(self, tmp_path: Path):
        from skill_eval.cli.commands.report import generate_report_html

        with patch.object(ReportGenerator, "write_static") as m:
            assert generate_report_html(tmp_path) is None
        m.assert_not_called()

    def test_missing_scorecard_ok(self, tmp_path: Path):
        from skill_eval.cli.commands.report import generate_report_html

        (tmp_path / "benchmark.json").write_text(
            json.dumps({"skill_name": "s"}), encoding="utf-8")
        captured = {}

        def fake_write_static(self, workspace, output_path, benchmark=None, previous=None, scorecard=None):  # noqa: ARG002
            captured["scorecard"] = scorecard
            output_path.write_text("x", encoding="utf-8")

        with patch.object(ReportGenerator, "write_static", fake_write_static):
            generate_report_html(tmp_path)
        assert captured["scorecard"] is None


class TestComplianceAggregate:
    """_aggregate_compliance:把 per-run compliance.json 按 side 聚合成整体遵循性,
    复用 audit.aggregate_by_instruction(= 把 N 个 transcript 喂给 audit 多会话审计的等价结果)。"""

    @staticmethod
    def _finding(text: str, verdict: str, transcript: str) -> dict:
        return {"instruction_text": text, "source": "CONSTRAINT", "verdict": verdict,
                "category": "prohibition", "evidence": "e", "confidence": 0.9, "transcript": transcript}

    def test_groups_by_side_same_instruction_across_runs(self):
        from skill_eval.reporting.viewer import _aggregate_compliance
        runs = [
            {"config": "with_skill", "compliance": {"findings": [self._finding("禁止编造", "PASS", "run-1.jsonl")]}},
            {"config": "with_skill", "compliance": {"findings": [self._finding("禁止编造", "FAIL", "run-2.jsonl")]}},
            {"config": "without_skill"},  # baseline 无 compliance
        ]
        agg = _aggregate_compliance(runs)
        assert len(agg) == 1, "只 with_skill 一个 side 有 compliance(baseline 无)"
        assert agg[0]["side"] == "with_skill"
        # 同一指令跨 2 run:pass1/fail1
        bi = agg[0]["by_instruction"]
        assert len(bi) == 1
        assert bi[0]["instruction_text"] == "禁止编造"
        assert bi[0]["pass_count"] == 1 and bi[0]["fail_count"] == 1
        assert agg[0]["summary"]["pass"] == 1 and agg[0]["summary"]["fail"] == 1
        assert agg[0]["runs_contributed"] == 2

    def test_skips_all_empty_side(self):
        from skill_eval.reporting.viewer import _aggregate_compliance
        # 一个 side 全空 digest(L1 理解题,findings=[])→ 不产空聚合
        runs = [{"config": "with_skill", "compliance": {"summary": {"total": 0}, "findings": []}}]
        assert _aggregate_compliance(runs) == []

    def test_mixed_empty_and_real_only_counts_real(self):
        from skill_eval.reporting.viewer import _aggregate_compliance
        # eval-1/2 空 digest + eval-3 真判定,同 side → 聚合只反映真判定的 run
        runs = [
            {"config": "with_skill", "compliance": {"findings": []}},  # L1 空
            {"config": "with_skill", "compliance": {"findings": [self._finding("必须读日志", "PASS", "eval3-r1.jsonl")]}},
        ]
        agg = _aggregate_compliance(runs)
        assert len(agg) == 1
        assert agg[0]["summary"]["pass"] == 1 and agg[0]["summary"]["total"] == 1
