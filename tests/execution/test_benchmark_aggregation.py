"""Benchmark aggregation, comparison, and calibration plumbing tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import sift.execution.comparison_suite as comparison_suite
from sift.execution.benchmark import BenchmarkAggregator
from sift.execution.comparison_suite import (
    ComparisonInputs,
    ScoringCallbacks,
    _compare_with_retry,
    run_comparisons,
)
from sift.models import ComparisonResult
from sift.models.enums import CalibrationVerdict


class TestBenchmarkAggregator:
    @staticmethod
    def test_aggregate_from_iteration(tmp_path: Path):
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下

        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            _write_grading_run(
                config_dir,
                json.dumps(
                    _grading_payload(
                        expectations=[_expectation_result(passed=config == "with_skill")],
                        summary=_grading_summary(
                            passed=1 if config == "with_skill" else 0,
                            failed=0 if config == "with_skill" else 1,
                            pass_rate=1.0 if config == "with_skill" else 0.0,
                        ),
                    )
                ),
                encoding="utf-8",
            )
            (config_dir / "timing.json").write_text(
                json.dumps(_run_timing()),
                encoding="utf-8",
            )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        assert len(benchmark.runs) == 2
        assert benchmark.run_summary["delta"]["pass_rate"] == "+1.00"

    @staticmethod
    def test_real_model_from_transcript(tmp_path):
        """从 transcript.jsonl 的 result event.modelUsage key 抓真实模型名。"""
        from sift.execution.benchmark import _real_model_from_transcript

        rd = tmp_path / "run-1"
        rd.mkdir()
        (rd / "transcript.jsonl").write_text(
            (
                '{"type":"assistant","message":{"model":"x"}}\n'
                '{"type":"result","modelUsage":{"glm-5.2[1m]":{"inputTokens":100}}}\n'
            ),
            encoding="utf-8",
        )
        assert _real_model_from_transcript(rd) == "glm-5.2[1m]"
        assert _real_model_from_transcript(tmp_path / "nope") == ""  # 无 transcript
        rd2 = tmp_path / "run-2"
        rd2.mkdir()
        (rd2 / "transcript.jsonl").write_text(('{"type":"assistant","message":{"model":"x"}}'), encoding="utf-8")
        assert _real_model_from_transcript(rd2) == ""  # 无 result event

    @staticmethod
    def test_aggregate_fills_executor_model_from_transcript(tmp_path: Path):
        """aggregate 从 transcript 抓真实模型名填 metadata.executor_model。
        comparator/analyzer 同源复用(真实 run 验证,单测环境 comparison/analysis 跳过)。
        """
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        config_dir = iteration / "with_skill" / "run-1"
        _write_grading_run(config_dir, json.dumps(_grading_payload(expectations=[])), encoding="utf-8")
        (config_dir / "transcript.jsonl").write_text(
            ('{"type":"result","modelUsage":{"glm-5.2[1m]":{"inputTokens":100}}}'),
            encoding="utf-8",
        )
        benchmark = _aggregate_fixture(iteration, tmp_path)
        assert benchmark.metadata.executor_model == "glm-5.2[1m]"

    @staticmethod
    def test_aggregate_evals_run_from_runs_not_single_checkpoints(tmp_path: Path):
        """evals_run 用 runs(所有已聚合 eval),不从单个 checkpoints.json 推。

        回归:_load_checkpoints 只读**首个** checkpoints.json(单 run、只见自己 eval),
        旧 `eval_ids = ... checkpoints_data if checkpoints_data else runs` 会短路到 checkpoints
        → evals_run 只含那个 run 的 eval(如 [1]),漏掉其余 eval。真实多 eval run 应得完整列表。
        """
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        config = iteration / "with_skill"
        for eval_id in (1, 2, 3):
            rd = config / f"eval-{eval_id}" / "run-1"
            _write_grading_run(rd, json.dumps(_grading_payload(expectations=[])), encoding="utf-8")
        # 首个 run 落一个只含 eval_id=1 的 checkpoints.json → 触发旧 checkpoints 短路分支
        (config / "eval-1" / "run-1" / "checkpoints.json").write_text(
            json.dumps([{"eval_id": 1, "step": "x"}]), encoding="utf-8"
        )
        benchmark = _aggregate_fixture(iteration, tmp_path)
        assert benchmark.metadata.evals_run == [1, 2, 3]

    @staticmethod
    def test_aggregate_dual_rate_delta(tmp_path: Path):
        """双率:grading summary 含 pass_rate_programmatic/_llm → run_summary + delta 双率。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        summaries = {
            "with_skill": {
                "passed": 2,
                "failed": 1,
                "total": 3,
                "pass_rate": 2 / 3,
                "pass_rate_programmatic": 1.0,
                "pass_rate_llm": 0.5,
            },
            "without_skill": {
                "passed": 1,
                "failed": 2,
                "total": 3,
                "pass_rate": 1 / 3,
                "pass_rate_programmatic": 0.5,
                "pass_rate_llm": 0.0,
            },
        }
        for config, summary in summaries.items():
            config_dir = iteration / config / "run-1"
            _write_grading_run(config_dir, json.dumps(_grading_payload(summary=summary)), encoding="utf-8")
            (config_dir / "timing.json").write_text(
                json.dumps(_run_timing()),
                encoding="utf-8",
            )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        ws = benchmark.run_summary["with_skill"]
        assert ws["pass_rate_programmatic"]["mean"] == 1.0
        assert ws["pass_rate_llm"]["mean"] == 0.5
        d = benchmark.run_summary["delta"]
        assert d["pass_rate_programmatic"] == "+0.50"  # with 1.0 - without 0.5
        assert d["pass_rate_llm"] == "+0.50"  # with 0.5 - without 0.0

    @staticmethod
    def test_aggregate_runs_carry_dual_rate_per_run(tmp_path: Path):
        """per-(eval,run) 双率透到 runs[].result(不只 run_summary 聚合):
        检查点总览 + 逐项表的 3-dim 列要 per-eval per-run 可见。
        RED:旧 runs[].result 只有 overall pass_rate;split 只进 entry_data→configs_data(Δ 卡)。
        """
        iteration = tmp_path
        config_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(
            config_dir,
            json.dumps(
                _grading_payload(
                    summary={
                        "passed": 2,
                        "failed": 1,
                        "total": 3,
                        "pass_rate": 2 / 3,
                        "pass_rate_programmatic": 1.0,
                        "pass_rate_llm": 0.5,
                    }
                )
            ),
            encoding="utf-8",
        )
        (config_dir / "timing.json").write_text(
            json.dumps(_run_timing()),
            encoding="utf-8",
        )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        r = benchmark.runs[0].result
        assert abs(r.pass_rate_programmatic - 1.0) < 1e-9
        assert abs(r.pass_rate_llm - 0.5) < 1e-9

    @staticmethod
    def test_aggregate_eval_name_from_run_debug(tmp_path: Path):
        """eval_name 从 run_debug.json 取真名(如 happy_path_basic_categorization),
        不再用 skill 名。检查点总览 + 逐项表靠它显题名,否则全显 "Eval N"。
        RED:旧代码 eval_name=name(skill 名,parse_skill_md 来),run_debug 没读。
        """
        iteration = tmp_path
        run_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(run_dir, json.dumps(_grading_payload()), encoding="utf-8")
        (run_dir / "timing.json").write_text(
            json.dumps(_run_timing()),
            encoding="utf-8",
        )
        (run_dir / "run_debug.json").write_text(
            json.dumps(
                {
                    "eval_name": "happy_path_basic_categorization",
                    "eval_id": 1,
                }
            ),
            encoding="utf-8",
        )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        assert benchmark.runs[0].eval_name == "happy_path_basic_categorization"

    @staticmethod
    def test_aggregate_eval_name_empty_when_no_run_debug(tmp_path: Path):
        """无 run_debug.json → eval_name 空(viewer fallback "Eval {id}"),不用 skill 名顶替。"""
        iteration = tmp_path
        run_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(run_dir, json.dumps(_grading_payload(expectations=[])), encoding="utf-8")
        (run_dir / "timing.json").write_text(
            json.dumps(_run_timing()),
            encoding="utf-8",
        )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        assert benchmark.runs[0].eval_name == ""

    @staticmethod
    def test_run_comparison_analysis_handles_semantic_winner(tmp_path: Path):
        """run_comparison_analysis 认语义 winner(with/without),不只老 A/B。
        RED:comparator 存语义 winner(position 随机化 + _translate 后),旧代码只判 ==A/==B
        → 恒 None → 深度分析永远不显示。改后 winner='with' 正常往下走到 analyze_comparison。
        """
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# body\n")
        ws = tmp_path / "with_skill" / "run-1"
        ws.mkdir(parents=True)
        (ws / "transcript.md").write_text("ws", encoding="utf-8")
        wos = tmp_path / "without_skill" / "run-1"
        wos.mkdir(parents=True)
        (wos / "transcript.md").write_text("wos", encoding="utf-8")

        benchmark = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )
        sentinel = object()
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: sentinel})()

        result = run_comparison_analysis(tmp_path, benchmark, analyzer)
        assert result is sentinel  # 旧代码这里 None(winner 'with' 不是 A/B)

    @staticmethod
    def test_run_comparison_analysis_semantic_winner_mapping(tmp_path: Path):
        """语义 winner 正确映射 winner/loser transcript:with→ws 赢、without→wos 赢。"""
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
        _comparison_transcripts(tmp_path)

        captured: dict = {}

        def fake(**kw):
            captured.update(kw)
            return "OK"

        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: fake(**kw)})()

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )
        run_comparison_analysis(tmp_path, bd, analyzer)
        assert captured["comparison"].winner_transcript == "WS"  # with 赢 → winner 是 ws

        captured.clear()
        bd2 = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="without", reasoning="r")],
        )
        run_comparison_analysis(tmp_path, bd2, analyzer)
        assert captured["comparison"].winner_transcript == "WOS"  # without 赢 → winner 是 wos

    @staticmethod
    def test_run_comparison_analysis_winner_by_overall_majority(tmp_path: Path):
        """winner 不再取 comparisons[0],而是**整体多数票**(排除 failed/tie)。
        场景:comparisons[0] 是 without 赢(与整体反),但整体 with 多 → winner 应=with,
        代表判词也取 with 侧赢的、不是 comparisons[0] 的 without 判词。
        RED:旧代码 winner=comparisons[0].winner → 'without'(反整体)。
        """
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
        _comparison_transcripts(tmp_path)

        captured: dict = {}
        analyzer = type(
            "A",
            (),
            {"analyze_comparison": lambda self, **kw: captured.update(kw) or "OK"},
        )()

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[
                ComparisonResult(winner="without", reasoning="comparisons[0] 是 without 赢"),  # 反整体
                ComparisonResult(winner="with", reasoning="with 赢 case A"),
                ComparisonResult(winner="with", reasoning="with 赢 case B"),
                ComparisonResult(winner="with", reasoning="with 赢 case C"),
            ],  # with 3 > without 1
        )
        run_comparison_analysis(tmp_path, bd, analyzer)
        assert captured["comparison"].winner == "with"  # 整体多数票,不是 comparisons[0] 的 without
        assert captured["comparison"].winner_transcript == "WS"
        # 代表判词来自 with 侧赢的(不是 comparisons[0] 的 without 判词)
        assert "comparisons[0]" not in captured["comparison"].comparison_reasoning
        assert "with 赢 case" in captured["comparison"].comparison_reasoning
        # 整体摘要
        assert "[整体分布]" in captured["comparison"].comparison_reasoning
        assert "3" in captured["comparison"].comparison_reasoning  # with 3 次
        assert "1" in captured["comparison"].comparison_reasoning  # without 1 次

    @staticmethod
    def test_run_comparison_analysis_focus_skill_is_tested_skill(tmp_path: Path):
        """focus_skill 永远 = 被测 skill(skill_content),不随 winner 侧变化:
        winner=with 时被测 skill 在 winner 位,winner=without 时在 loser 位,
        但 focus_skill 两场景都该 = 被测 skill 全文(改进对象恒定)。
        """
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# 被测 skill body\n")
        _comparison_transcripts(tmp_path)

        captured: dict = {}
        analyzer = type(
            "A",
            (),
            {"analyze_comparison": lambda self, **kw: captured.update(kw) or "OK"},
        )()

        # winner=with(被测 skill 在 winner 位)
        bd_with = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )
        run_comparison_analysis(tmp_path, bd_with, analyzer)
        assert "被测 skill body" in captured["comparison"].focus_skill

        captured.clear()
        # winner=without(被测 skill 在 loser 位)
        bd_without = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="without", reasoning="r")],
        )
        run_comparison_analysis(tmp_path, bd_without, analyzer)
        assert "被测 skill body" in captured["comparison"].focus_skill  # 仍是被测 skill,不是 "(no skill)"

    @staticmethod
    def test_run_comparison_analysis_tie_returns_none(tmp_path: Path):
        """整体平局(with==without)→ 无代表性强弱,返 None 不分析。
        RED:旧代码 winner=comparisons[0].winner='with' → 不 None 继续分析。
        """
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[
                ComparisonResult(winner="with"),
                ComparisonResult(winner="without"),
            ],  # 平局
        )
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: "OK"})()
        assert run_comparison_analysis(tmp_path, bd, analyzer) is None

    @staticmethod
    def test_run_comparison_analysis_no_valid_returns_none(tmp_path: Path):
        """全 tie/failed → 无有效胜负,返 None(回归保护)。"""
        from sift.execution.analysis_suite import run_comparison_analysis
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[
                ComparisonResult(winner="tie"),
                ComparisonResult(winner="tie", failed=True),
            ],
        )
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: "OK"})()
        assert run_comparison_analysis(tmp_path, bd, analyzer) is None

    @staticmethod
    def test_run_comparison_analysis_raises_on_transient_none(tmp_path: Path):
        """analyze_comparison 被调用(已过结构检查:清晰赢家+有 transcript)却返 None
        (瞬时:http 抖动/空响应/偶发不产 tool_use)→ 抛 _ComparisonAnalysisTransient
        交 run_analysis 重试循环兜底,而非静默 None 丢深度分析。区别于结构性 None
        (平局/无赢家/无比较,上方 early-return、不进这里、不抛——既有测试覆盖)。
        """
        from sift.execution.analysis_suite import (
            _ComparisonAnalysisTransient,
            run_comparison_analysis,
        )
        from sift.models import BenchmarkData, BenchmarkMetadata

        skill_dir = _comparison_skill(tmp_path, "---\nname: s\ndescription: d\n---\n# b\n")
        _comparison_transcripts(tmp_path)

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )  # 清晰赢家 → 进 analyze_comparison
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: None})()  # 瞬时:返 None
        with pytest.raises(_ComparisonAnalysisTransient):
            run_comparison_analysis(tmp_path, bd, analyzer)

    @staticmethod
    def test_run_analysis_retries_transient_analysis_failure(tmp_path: Path):
        """run_comparison_analysis 第一次抛 _ComparisonAnalysisTransient(瞬时)、第二次
        成功 → run_analysis 重试循环兜底 → 返回非 None analysis(一次抖动不再丢深度分析)。
        """
        from sift.execution.analysis_suite import (
            _ComparisonAnalysisTransient,
            run_analysis,
        )
        from sift.models import AnalysisResult, BenchmarkData, BenchmarkMetadata
        from sift.models.benchmark_models import RunResultEntry, RunResultMetrics

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s"),
            runs=[
                RunResultEntry(
                    eval_id=1,
                    configuration="with_skill",
                    run_number=1,
                    result=RunResultMetrics(pass_rate=1.0, passed=1, failed=0, total=1),
                )
            ],
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )
        sentinel = AnalysisResult(winner_strengths=["s"])  # 真 AnalysisResult(run_analysis 会 model_dump 落盘)
        agg = BenchmarkAggregator(model=None, timeout=10)
        with (
            patch("sift.scoring.analyzer.PosthocAnalyzer") as mock_analyzer,
            patch(
                ("sift.execution.analysis_suite.run_comparison_analysis"),
                side_effect=[_ComparisonAnalysisTransient("flake"), sentinel],
            ),
        ):
            mock_analyzer.return_value.analyze_benchmark.return_value = []
            notes, analysis = run_analysis(agg, tmp_path, bd)
        assert analysis is sentinel  # 重试后成功(旧代码:第一次 None → break → 永远丢)

    @staticmethod
    def test_aggregate_dual_rate_null_when_class_missing(tmp_path: Path):
        """某类 expectation 全无(pass_rate_xx 缺)→ 该双率 stats None、delta 该项 None。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            _write_grading_run(
                config_dir,
                json.dumps(
                    _grading_payload(
                        expectations=[{"text": "t", "passed": True, "evidence": "e", "source": "programmatic"}],
                        summary={"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0, "pass_rate_programmatic": 1.0},
                    )
                ),
                encoding="utf-8",
            )
            (config_dir / "timing.json").write_text(
                json.dumps(_run_timing()),
                encoding="utf-8",
            )

        benchmark = _aggregate_fixture(iteration, tmp_path)

        ws = benchmark.run_summary["with_skill"]
        assert ws["pass_rate_programmatic"]["mean"] == 1.0
        assert ws["pass_rate_llm"] is None  # 无 llm expectation
        assert benchmark.run_summary["delta"]["pass_rate_llm"] is None

    @staticmethod
    def test_aggregate_fills_error_triage_from_grading_error(tmp_path: Path):
        """grading.json 含 error → aggregate 填 RunResultEntry.error_root_cause/suggestion;
        无 error → None(向后兼容)。纯诊断字段,不进 pass_rate 等判决。
        """
        iteration = tmp_path  # run 树根即 output_dir
        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            config_dir.mkdir(parents=True)
            (config_dir / "outputs").mkdir()
            grading = _grading_payload(
                expectations=[{"text": "t", "passed": False, "evidence": "e", "source": "error"}],
                summary=_grading_summary(passed=0, failed=1, pass_rate=0.0),
            )
            if config == "with_skill":  # 只有 with 侧有 error
                grading["error"] = "claude -p timed out after 120s (killed process tree)"
            (config_dir / "grading.json").write_text(json.dumps(grading), encoding="utf-8")
            (config_dir / "timing.json").write_text(
                json.dumps(_run_timing()),
                encoding="utf-8",
            )
        benchmark = _aggregate_fixture(iteration, tmp_path)
        by_config = {r.configuration: r for r in benchmark.runs}
        assert by_config["with_skill"].error_root_cause == "timeout"
        assert by_config["with_skill"].error_suggestion  # 非空建议
        assert by_config["without_skill"].error_root_cause is None  # 无 error → None
        assert by_config["without_skill"].error_suggestion is None

    @staticmethod
    def test_aggregate_ignores_superseded_retry_base(tmp_path: Path):
        """benchmark 聚合 retry-glob bug 回归:被 retry 取代的失败 base 不当独立 run 计入。
        with eval-1 有 run-1(base 0.0)+ run-1-retry-1(最终 0.75),聚合应只算 retry-1。
        旧 glob("eval-*/run-*") 两条都读 → with 均值被失败 base 拖低 → delta 失真。
        """
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下

        def make(run_dir: Path, pass_rate: float) -> None:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "outputs").mkdir(exist_ok=True)
            (run_dir / "grading.json").write_text(
                json.dumps(
                    _grading_payload(
                        expectations=[_expectation_result(passed=pass_rate >= 0.5)],
                        summary={
                            "passed": 0,
                            "failed": 1,
                            "total": 1,
                            "pass_rate": pass_rate,
                            "pass_rate_llm": pass_rate,
                        },
                    )
                ),
                encoding="utf-8",
            )
            (run_dir / "timing.json").write_text(
                json.dumps(_run_timing()),
                encoding="utf-8",
            )

        # with:eval-1 run-1 base(失败)+ run-1-retry-1(最终);eval-2 run-1
        make(iteration / "with_skill" / "eval-1" / "run-1", 0.0)
        make(iteration / "with_skill" / "eval-1" / "run-1-retry-1", 0.75)
        make(iteration / "with_skill" / "eval-2" / "run-1", 1.0)
        # without:eval-1/eval-2 各 run-1(无 retry)
        make(iteration / "without_skill" / "eval-1" / "run-1", 0.25)
        make(iteration / "without_skill" / "eval-2" / "run-1", 0.5)

        benchmark = _aggregate_fixture(iteration, tmp_path)

        # with 只 2 条最终尝试(eval-1/run-1→retry 0.75 + eval-2/run-1 1.0),非 3 条
        assert len(benchmark.runs) == 4  # with 2 + without 2,不含被取代的 base
        assert benchmark.run_summary["with_skill"]["pass_rate"]["mean"] == pytest.approx((0.75 + 1.0) / 2)
        # delta 用最终尝试:with mean 0.875 - without mean 0.375 = +0.50
        assert benchmark.run_summary["delta"]["pass_rate"] == "+0.50"

    @staticmethod
    def test_aggregate_empty(tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        iteration.mkdir()

        agg = BenchmarkAggregator()
        with (
            patch("sift.execution.benchmark.run_comparisons", return_value=[]),
            patch("sift.execution.benchmark.run_analysis", return_value=([], None)),
        ):
            benchmark = agg.aggregate(iteration)

        assert benchmark.runs == []

    @staticmethod
    def test_aggregate_survives_comparison_failure(tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(
            with_skill_dir,
            json.dumps(
                {
                    "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
                    "expectations": [_expectation_result(text="x", evidence="")],
                }
            ),
        )

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        _write_grading_run(
            without_skill_dir,
            json.dumps(
                {
                    "summary": {"pass_rate": 0.0, "passed": 0, "failed": 2, "total": 2},
                    "expectations": [_expectation_result(text="x", passed=False, evidence="")],
                }
            ),
        )

        agg = BenchmarkAggregator()
        with patch(
            "sift.execution.benchmark.run_comparisons",
            side_effect=RuntimeError("LLM timeout"),
        ):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert "with_skill" in benchmark.run_summary
        assert benchmark.comparisons is None or benchmark.comparisons == []

    @staticmethod
    def test_aggregate_survives_analysis_failure(tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(
            with_skill_dir,
            json.dumps({"summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2}, "expectations": []}),
        )

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        _write_grading_run(
            without_skill_dir,
            json.dumps({"summary": {"pass_rate": 0.5, "passed": 1, "failed": 1, "total": 2}, "expectations": []}),
        )

        agg = BenchmarkAggregator()
        with (
            patch("sift.execution.benchmark.run_comparisons", return_value=[]),
            patch(
                "sift.execution.benchmark.run_analysis",
                side_effect=RuntimeError("LLM rate limited"),
            ),
        ):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert benchmark.analysis is None

    @staticmethod
    def test_aggregate_survives_both_failures(tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(
            with_skill_dir,
            json.dumps({"summary": {"pass_rate": 0.8, "passed": 4, "failed": 1, "total": 5}, "expectations": []}),
        )

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        _write_grading_run(
            without_skill_dir,
            json.dumps({"summary": {"pass_rate": 0.2, "passed": 1, "failed": 4, "total": 5}, "expectations": []}),
        )

        agg = BenchmarkAggregator()
        with (
            patch(
                "sift.execution.benchmark.run_comparisons",
                side_effect=RuntimeError("fail1"),
            ),
            patch(
                "sift.execution.benchmark.run_analysis",
                side_effect=RuntimeError("fail2"),
            ),
        ):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert benchmark.comparisons is None or benchmark.comparisons == []
        assert benchmark.analysis is None


class TestComparisonSkipWhenProgrammaticOnly:
    """④ 盲比较 programmatic-only 跳过:enable_llm=False 时 run_comparisons 直接返回 []。"""

    @staticmethod
    def test_skips_when_enable_llm_false(tmp_path: Path):
        # with/without run 目录齐备(若无门禁会进入比较流程)
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        agg = BenchmarkAggregator(model="m", enable_llm=False)
        with patch(("sift.execution.comparison_suite.BlindComparator")) as mock_cmp:
            assert run_comparisons(agg, tmp_path) == []
        mock_cmp.assert_not_called()  # 门禁在 BlindComparator 实例化之前

    @staticmethod
    def test_proceeds_when_enable_llm_true(tmp_path: Path):
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        agg = BenchmarkAggregator(model="m", enable_llm=True)
        with patch(("sift.execution.comparison_suite.BlindComparator")) as mock_cmp:
            mock_cmp.return_value.compare.return_value = ComparisonResult(winner="with", reasoning="t")
            result = run_comparisons(agg, tmp_path)
        assert mock_cmp.called  # 门禁放行,BlindComparator 被实例化
        assert len(result) == 1


class TestComparisonSingleArm:
    """agent 评测 sides(绝对质量,长度 1)→ run_comparisons 早返 [] 不 IndexError
    (盲比较需两臂对照,agent 评测无对照)。
    """

    @staticmethod
    def test_single_arm_returns_empty_no_crash(tmp_path: Path):
        agg = BenchmarkAggregator(model="m", enable_llm=True)
        sides = [("agent", tmp_path / "agent", "agent")]
        with patch(("sift.execution.comparison_suite.BlindComparator")) as mock_cmp:
            assert run_comparisons(agg, tmp_path, sides=sides) == []
        mock_cmp.assert_not_called()  # agent 评测守卫在 BlindComparator 实例化之前


class TestComparisonPairing:
    """配对 bug 回归:run_comparisons 必须按 (eval, run) 显式配对、
    with 侧有 retry 目录时取最终 retry,不能 glob+zip 错位(原 bug:retry 目录混入
    glob → 排序后 zip 把 with 多出的 retry 配给 without 的下一个 eval → 跨 eval 伪 tie)。
    """

    @staticmethod
    def _fake_comparator_factory(calls: list, markers: tuple[str, ...]):
        class _FakeComparator:
            def __init__(self, *a, **kw):
                pass

            @staticmethod
            def compare(comparison, **kwargs):
                output_a = comparison.output_a
                output_b = comparison.output_b
                calls.append((output_a, output_b))
                return ComparisonResult(winner="with", reasoning="ok")

        def _tok(s: str) -> str:
            return next(m for m in markers if m in s)

        return _FakeComparator, _tok

    def test_pairs_by_eval_and_run_taking_final_retry(self, tmp_path: Path, monkeypatch):
        """with 侧 eval-1 有 run-1(base)+ run-1-retry-1(最终应取 retry-1)。
        旧 glob+zip:ws_runs=[e1/r1, e1/r1-retry1, e2/r1] 配 wos_runs=[e1/r1, e2/r1]
        → 错位成 [(base, wos-e1), (retry, wos-e2)] 跨 eval。修复后按 (eval,run) 对齐。
        """
        markers = ("W-E1-R1-BASE", "W-E1-R1-RETRY", "W-E2-R1", "WO-E1-R1", "WO-E2-R1")
        calls: list[tuple[str, str]] = []
        fake_cmp, _tok = self._fake_comparator_factory(calls, markers)
        monkeypatch.setattr(comparison_suite, "BlindComparator", fake_cmp)

        # with:eval-1 双层(base + retry)、eval-2 单层
        make_comparison_run(tmp_path / "with_skill" / "eval-1" / "run-1", "W-E1-R1-BASE")
        make_comparison_run(tmp_path / "with_skill" / "eval-1" / "run-1-retry-1", "W-E1-R1-RETRY")
        make_comparison_run(tmp_path / "with_skill" / "eval-2" / "run-1", "W-E2-R1")
        # without:无 retry
        make_comparison_run(tmp_path / "without_skill" / "eval-1" / "run-1", "WO-E1-R1")
        make_comparison_run(tmp_path / "without_skill" / "eval-2" / "run-1", "WO-E2-R1")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path)
        assert len(results) == 2  # 只 (eval-1,run-1) + (eval-2,run-1) 两对

        pairings = sorted((_tok(w), _tok(wo)) for w, wo in calls)
        assert pairings == [
            ("W-E1-R1-RETRY", "WO-E1-R1"),  # eval-1/run-1:with 取最终 retry-1,非 base
            ("W-E2-R1", "WO-E2-R1"),  # eval-2/run-1:各自对齐
        ]
        all_with = [_tok(c[0]) for c in calls]
        assert "W-E1-R1-BASE" not in all_with  # retry 覆盖 base,base 不进比较

    def test_pairs_flat_structure_no_retry(self, tmp_path: Path, monkeypatch):
        """flat 结构(with_skill/run-N,无 eval 分层)配对不回归。"""
        markers = ("W-R1", "W-R2", "WO-R1", "WO-R2")
        calls: list[tuple[str, str]] = []
        fake_cmp, _tok = self._fake_comparator_factory(calls, markers)
        monkeypatch.setattr(comparison_suite, "BlindComparator", fake_cmp)

        make_comparison_run(tmp_path / "with_skill" / "run-1", "W-R1")
        make_comparison_run(tmp_path / "with_skill" / "run-2", "W-R2")
        make_comparison_run(tmp_path / "without_skill" / "run-1", "WO-R1")
        make_comparison_run(tmp_path / "without_skill" / "run-2", "WO-R2")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path)
        assert len(results) == 2
        pairings = sorted((_tok(w), _tok(wo)) for w, wo in calls)
        assert pairings == [("W-R1", "WO-R1"), ("W-R2", "WO-R2")]


class TestComparisonSides:
    """块 B:sides 驱动 run_comparisons —— 读 side dir + 透 comp_label 给 comparator。"""

    @staticmethod
    def _make_run(base: Path, marker: str) -> None:
        (base / "outputs").mkdir(parents=True, exist_ok=True)
        (base / "outputs" / "out.txt").write_text(marker, encoding="utf-8")

    def test_v1v2_sides_drive_dirs_and_labels(self, tmp_path: Path, monkeypatch):
        seen_labels: list[tuple] = []

        class _FakeComparator:
            def __init__(self, *a, **kw):
                pass

            @staticmethod
            def compare(comparison, **kwargs):
                label_a = comparison.label_a
                label_b = comparison.label_b
                seen_labels.append((label_a, label_b))
                return ComparisonResult(winner=label_a, reasoning="ok")  # side A 胜

        monkeypatch.setattr(comparison_suite, "BlindComparator", _FakeComparator)

        self._make_run(tmp_path / "v1" / "eval-1" / "run-1", "V1-OUT")
        self._make_run(tmp_path / "v2" / "eval-1" / "run-1", "V2-OUT")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path, sides=[("v1", None, "v1"), ("v2", None, "v2")])
        assert len(results) == 1
        assert results[0].winner == "v1"  # winner 用 v1 label,非 with
        assert seen_labels == [("v1", "v2")]  # comp_label 透到 comparator

    def test_default_sides_none_falls_back_to_with_without(self, tmp_path: Path, monkeypatch):
        """sides=None → 默认 with_skill/without_skill dir + with/without label(向后兼容)。"""

        class _FakeComparator:
            def __init__(self, *a, **kw):
                pass

            @staticmethod
            def compare(comparison, **kwargs):
                label_a = comparison.label_a
                return ComparisonResult(winner=label_a, reasoning="ok")

        monkeypatch.setattr(comparison_suite, "BlindComparator", _FakeComparator)
        self._make_run(tmp_path / "with_skill" / "eval-1" / "run-1", "W")
        self._make_run(tmp_path / "without_skill" / "eval-1" / "run-1", "WO")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path)  # sides 默认 None
        assert len(results) == 1
        assert results[0].winner == "with"  # 默认 label


class TestComparisonRetry:
    """_compare_with_retry 重试机制:compare() 返回 failed=True(调用/解析失败)时重试。
    旧实现 try/except 重试,但 compare() 把失败吞成 TIE 永不抛 → 重试循环是死代码。
    """

    @staticmethod
    def test_retries_until_success():
        """前 2 次 failed、第 3 次成功 → 重试到成功即返回。"""
        attempts = []

        class _Flaky:
            @staticmethod
            def compare(comparison, **kwargs):
                attempts.append(len(attempts) + 1)
                if len(attempts) < 3:
                    return ComparisonResult(winner="tie", reasoning="无法解析", failed=True)
                return ComparisonResult(winner="with", reasoning="ok", failed=False)

        with patch.object(comparison_suite, "time"):  # 跳过 backoff sleep
            result = _compare_with_retry(
                comparator=_Flaky(),
                max_retries=3,
                comparison=ComparisonInputs(output_a="ws", output_b="wos", eval_prompt="ep", expectations=None),
                callbacks=ScoringCallbacks(),
            )
        assert result.winner == "with"
        assert len(attempts) == 3  # 重试了 2 次

    @staticmethod
    def test_exhausts_to_none_when_all_fail():
        """每次都 failed → 耗尽 max_retries 返回 None(由调用方降级 tie)。"""
        attempts = []

        class _AlwaysFail:
            @staticmethod
            def compare(comparison, **kwargs):
                attempts.append(1)
                return ComparisonResult(winner="tie", reasoning="对比调用失败", failed=True)

        with patch.object(comparison_suite, "time"):
            result = _compare_with_retry(
                comparator=_AlwaysFail(),
                max_retries=3,
                comparison=ComparisonInputs(output_a="ws", output_b="wos", eval_prompt="ep", expectations=None),
                callbacks=ScoringCallbacks(),
            )
        assert result is None
        assert len(attempts) == 3

    @staticmethod
    def test_no_retry_on_success():
        """成功(failed=False)→ 立即返回,不重试。"""
        attempts = []

        class _Ok:
            @staticmethod
            def compare(comparison, **kwargs):
                attempts.append(1)
                return ComparisonResult(winner="with", reasoning="ok", failed=False)

        result = _compare_with_retry(
            comparator=_Ok(),
            max_retries=3,
            comparison=ComparisonInputs(output_a="ws", output_b="wos", eval_prompt="ep", expectations=None),
            callbacks=ScoringCallbacks(),
        )
        assert result.winner == "with"
        assert len(attempts) == 1

    @staticmethod
    def test_exhausted_degradation_marked_failed(tmp_path: Path, monkeypatch):
        """compare 恒 failed → _compare_with_retry 耗尽 None → run_comparisons 降级 tie 必带
        failed=True(否则失败混进 genuine-tie 桶,污染 win-rate 分母)。复用 TestComparisonPairing
        的 _make_run + run_comparisons 范式。
        """

        class _AlwaysFails:
            def __init__(self, *a, **kw):
                pass

            @staticmethod
            def compare(comparison, **kwargs):
                return ComparisonResult(winner="tie", reasoning="对比调用失败", failed=True)

        monkeypatch.setattr(comparison_suite, "BlindComparator", _AlwaysFails)
        make_comparison_run(tmp_path / "with_skill" / "run-1", "w")
        make_comparison_run(tmp_path / "without_skill" / "run-1", "wo")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        with patch.object(comparison_suite.time, "sleep"):  # 只跳过重试等待，保留真实计时
            results = run_comparisons(_Agg(), tmp_path)
        assert len(results) == 1
        assert results[0].failed is True  # 修复前为 False


class TestComparisonPoolIsolation:
    """单对 compare() 抛异常(非 failed)→ _do_comparison 兜成 failed tie、写
    comparison-*.json,整池不崩、其余对正常(独立脆弱点:不切 http 也该修)。
    """

    @staticmethod
    def _make_run(base: Path, marker: str) -> None:
        (base / "outputs").mkdir(parents=True, exist_ok=True)
        (base / "outputs" / "out.txt").write_text(marker, encoding="utf-8")

    def test_crashing_pair_becomes_failed_tie_pool_continues(self, tmp_path: Path, monkeypatch, caplog):
        class _CrashOnMarker:
            def __init__(self, *a, **kw):
                pass

            @staticmethod
            def compare(comparison, **kwargs):
                output_a = comparison.output_a
                if "CRASH" in output_a:
                    raise ValueError("simulated crash")
                return ComparisonResult(winner="with", reasoning="ok")

        monkeypatch.setattr(comparison_suite, "BlindComparator", _CrashOnMarker)
        self._make_run(tmp_path / "with_skill" / "run-1", "CRASH-MARKER")
        self._make_run(tmp_path / "without_skill" / "run-1", "wo")
        self._make_run(tmp_path / "with_skill" / "run-2", "ok")
        self._make_run(tmp_path / "without_skill" / "run-2", "wo")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path)  # 不抛
        assert len(results) == 2  # 整池完成(不击穿)
        crashed = next(r for r in results if r.failed)
        assert crashed.winner == "tie"  # 降级 tie
        ok = next(r for r in results if not r.failed)
        assert ok.winner == "with"  # 另一对正常返回
        # crashed 对仍写 comparison-*.json(failed tie),保 fs 一致
        cr1 = json.loads((tmp_path / "comparison-run1.json").read_text(encoding="utf-8"))
        assert cr1["failed"] is True and cr1["winner"] == "tie"
        cr2 = json.loads((tmp_path / "comparison-run2.json").read_text(encoding="utf-8"))
        assert cr2["winner"] == "with"
        assert "comparison crashed [with_skill/run-1]: ValueError: simulated crash" in caplog.text
        assert any(record.exc_info for record in caplog.records)  # 日志保留原异常堆栈，不静默吞错


class TestComparisonSummary:
    """ComparisonSummary.from_comparisons:win-rate 有效分母 = with+without+genuine tie,
    failed 排除(失败≠打平)。
    """

    @staticmethod
    def test_mixed_win_rate_excludes_failed():
        comps = [
            ComparisonResult(winner="with", reasoning=""),
            ComparisonResult(winner="without", reasoning=""),
            ComparisonResult(winner="tie", reasoning="等价"),  # genuine tie
            ComparisonResult(winner="tie", reasoning="耗尽", failed=True),  # 失败
        ]
        from sift.models import ComparisonSummary

        s = ComparisonSummary.from_comparisons(comps)
        assert s.total == 4
        assert (s.a_wins, s.b_wins, s.ties, s.failed) == (1, 1, 1, 1)
        assert s.valid == 3  # genuine tie 计入,failed 排除
        assert s.win_rate == 1 / 3
        assert s.label_a == "with" and s.label_b == "without"  # 默认 label

    @staticmethod
    def test_genuine_tie_counts_in_denominator():
        # 2 with / 0 without / 3 tie → win_rate 2/5(打平算一场比过)
        comps = [ComparisonResult(winner="with", reasoning="")] * 2 + [ComparisonResult(winner="tie", reasoning="")] * 3
        from sift.models import ComparisonSummary

        s = ComparisonSummary.from_comparisons(comps)
        assert s.win_rate == 2 / 5
        assert s.valid == 5

    @staticmethod
    def test_all_failed_win_rate_none():
        comps = [ComparisonResult(winner="tie", reasoning="x", failed=True) for _ in range(3)]
        from sift.models import ComparisonSummary

        s = ComparisonSummary.from_comparisons(comps)
        assert s.valid == 0
        assert s.win_rate is None  # 不报假 0%
        assert s.failed == 3

    @staticmethod
    def test_v1v2_label_bucketing():
        """v1/v2 模式:label_a/b 驱动分桶;win_rate 恒为 side A(v1)胜率。"""
        comps = [
            ComparisonResult(winner="v1", reasoning=""),
            ComparisonResult(winner="v2", reasoning=""),
            ComparisonResult(winner="v1", reasoning=""),
            ComparisonResult(winner="tie", reasoning="等价"),
        ]
        from sift.models import ComparisonSummary

        s = ComparisonSummary.from_comparisons(comps, label_a="v1", label_b="v2")
        assert (s.a_wins, s.b_wins, s.ties, s.failed) == (2, 1, 1, 0)
        assert s.valid == 4
        assert s.win_rate == 0.5  # a_wins(2)/valid(4)
        assert s.label_a == "v1" and s.label_b == "v2"

    @staticmethod
    def test_empty_returns_none():
        from sift.models import ComparisonSummary

        assert ComparisonSummary.from_comparisons([]) is None


class TestCalibrationAggregatorPlumbing:
    """Task 3:aggregator 把 per-run calibration.json 聚合成顶层 calibration_summary。
    聚焦 plumbing——直接测 _collect_per_side_calibration + CalibrationSummary.from_per_side,
    不端到端调完整 aggregate(避开 run_comparisons/run_analysis mock 噪音)。

    sides 真实结构:(config_name, inject_skill_path_or_None, label)——见 build_sides。
    config 目录 = iteration_dir / config_name(不在 tuple 里)。
    """

    @staticmethod
    def _calibration_json(verdict, hard_violations=None, compliance_rate=1.0) -> str:
        """造一份 calibration.json 内容,匹配 calibrator.py 的输出 schema。"""
        from sift.models import CalibrationReport
        from sift.models.enums import CalibrationVerdict, ProcessStatus

        result_pass = verdict in (
            CalibrationVerdict.TRUSTED,
            CalibrationVerdict.HIGH_SCORE_SUSPECT,
        )
        process_status = ProcessStatus.OK if verdict == CalibrationVerdict.TRUSTED else ProcessStatus.VIOLATED
        rep = CalibrationReport(
            verdict=verdict,
            result_pass=result_pass,
            process_status=process_status,
            hard_violations=hard_violations or [],
            soft_notes={},
            scoring_rate=0.9,
            scoring_rate_basis="programmatic",
            scoring_rate_programmatic=0.9,
            scoring_rate_llm=None,
            compliance_rate=compliance_rate,
        )
        return rep.model_dump_json(indent=2)

    def test_collect_per_side_and_aggregate(self, tmp_path: Path):
        """with_skill: eval-1/run-1 TRUSTED + eval-1/run-2 HIGH_SCORE_SUSPECT(带 hard_violations);
        without_skill: 空目录(无 calibration.json)。
        断言:with_skill.total==2、high_score_suspects 长度 1、without_skill is None。
        """
        from sift.execution.benchmark import _collect_per_side_calibration
        from sift.models import CalibrationSummary, HardViolation
        from sift.models.enums import CalibrationVerdict

        hv = HardViolation(
            category="prohibition",
            instruction_text="禁止编造错误码",
            evidence="E-FAKE-999",
        )
        # with_skill: 两份 calibration.json(一 TRUSTED、一 HIGH_SCORE_SUSPECT 带 hv)
        ws1 = tmp_path / "with_skill" / "eval-1" / "run-1"
        ws1.mkdir(parents=True)
        (ws1 / "calibration.json").write_text(self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        ws2 = tmp_path / "with_skill" / "eval-1" / "run-2"
        ws2.mkdir(parents=True)
        (ws2 / "calibration.json").write_text(
            self._calibration_json(
                CalibrationVerdict.HIGH_SCORE_SUSPECT,
                hard_violations=[hv],
                compliance_rate=0.5,
            ),
            encoding="utf-8",
        )
        # without_skill: 空目录(无 calibration.json)
        (tmp_path / "without_skill").mkdir(parents=True)

        # sides 真实结构:(config_name, inject_skill, label)
        sides = [
            ("with_skill", None, "with_skill"),
            ("without_skill", None, "without_skill"),
        ]
        per_side = _collect_per_side_calibration(tmp_path, sides)

        # 直接断言 per_side 结构(plumbing 契约)
        assert per_side["without_skill"] is None
        ws_reports = per_side["with_skill"]
        assert ws_reports is not None and len(ws_reports) == 2
        # (eval_id, run_n, CalibrationReport) 三元
        eval_run_pairs = sorted((eid, rn) for eid, rn, _ in ws_reports)
        assert eval_run_pairs == [(1, 1), (1, 2)]

        summary = CalibrationSummary.from_per_side(per_side)
        ws = summary.sides["with_skill"]
        assert ws.total == 2
        assert ws.verdict_counts == {"TRUSTED": 1, "HIGH_SCORE_SUSPECT": 1}
        assert len(ws.high_score_suspects) == 1
        sus = ws.high_score_suspects[0]
        assert (sus["eval"], sus["run"]) == (1, 2)
        assert sus["category"] == "prohibition"
        assert sus["instruction_text"] == "禁止编造错误码"
        assert sus["evidence"] == "E-FAKE-999"
        assert sus["compliance_rate"] == 0.5
        assert summary.sides["without_skill"] is None

    def test_collect_per_side_takes_final_retry(self, tmp_path: Path):
        """retry-glob bug 类回归:index_runs 取最终 retry 去重,base+retry 只算 retry。
        被 retry 取代的失败 base 不当独立 run 计入(与 aggregate / run_comparisons 同源)。
        """
        from sift.execution.benchmark import _collect_per_side_calibration
        from sift.models.enums import CalibrationVerdict

        # eval-1 有 run-1(base TRUSTED)+ run-1-retry-1(最终 HIGH_SCORE_SUSPECT)
        base = tmp_path / "with_skill" / "eval-1" / "run-1"
        base.mkdir(parents=True)
        (base / "calibration.json").write_text(self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        retry = tmp_path / "with_skill" / "eval-1" / "run-1-retry-1"
        retry.mkdir(parents=True)
        (retry / "calibration.json").write_text(
            self._calibration_json(
                CalibrationVerdict.HIGH_SCORE_SUSPECT,
                hard_violations=[
                    {
                        "category": "prohibition",
                        "instruction_text": "x",
                        "evidence": "y",
                    }
                ],
            ),
            encoding="utf-8",
        )

        sides = [("with_skill", None, "with")]
        per_side = _collect_per_side_calibration(tmp_path, sides)
        ws = per_side["with"]
        assert ws is not None and len(ws) == 1, "base+retry 只算最终 retry 一条,不当两条独立 run"
        # 取的是 retry-1(HIGH_SCORE_SUSPECT),不是 base(TRUSTED)
        _eid, _rn, rep = ws[0]
        assert rep.verdict == CalibrationVerdict.HIGH_SCORE_SUSPECT

    def test_collect_per_side_skips_corrupt_json(self, tmp_path: Path):
        """calibration.json 损坏(非合法 JSON / schema 不符)→ 静默跳过,不抛。
        与 aggregate 主循环同款防御(grading.json 解析失败也跳过)。
        """
        from sift.execution.benchmark import _collect_per_side_calibration

        good = tmp_path / "with_skill" / "eval-1" / "run-1"
        good.mkdir(parents=True)
        (good / "calibration.json").write_text(
            self._calibration_json(CalibrationVerdict.TRUSTED),
            encoding="utf-8",
        )
        bad = tmp_path / "with_skill" / "eval-1" / "run-2"
        bad.mkdir(parents=True)
        (bad / "calibration.json").write_text("NOT JSON {{{", encoding="utf-8")

        per_side = _collect_per_side_calibration(tmp_path, [("with_skill", None, "with")])
        ws = per_side["with"]
        assert ws is not None and len(ws) == 1, "corrupt 那条跳过,只留 good 一条"

    def test_aggregate_sets_calibration_summary(self, tmp_path: Path):
        """端到端 sanity:aggregate() 把 calibration_summary 钉到 BenchmarkData。
        与 comparison_summary 对称(顶层 summary 字段)。
        """
        from sift.models import CalibrationSummary
        from sift.models.enums import CalibrationVerdict

        # 造最小 run 树(让 aggregate 不崩)+ calibration.json
        ws = tmp_path / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(ws, json.dumps(_grading_payload()), encoding="utf-8")
        (ws / "calibration.json").write_text(self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        _write_without_calibration_run(tmp_path)

        agg = BenchmarkAggregator()
        with (
            patch("sift.execution.benchmark.run_comparisons", return_value=[]),
            patch("sift.execution.benchmark.run_analysis", return_value=([], None)),
        ):
            benchmark = agg.aggregate(tmp_path, tmp_path)

        assert benchmark.calibration_summary is not None
        assert isinstance(benchmark.calibration_summary, CalibrationSummary)
        ws_side = benchmark.calibration_summary.sides.get("with")
        assert ws_side is not None and ws_side.total == 1
        assert ws_side.verdict_counts == {"TRUSTED": 1}
        # without_skill 侧无 calibration.json → None
        assert benchmark.calibration_summary.sides.get("without") is None

    @staticmethod
    def test_aggregate_survives_calibration_failure(tmp_path: Path):
        """calibration 收集抛异常 → 不崩 aggregate,calibration_summary 留 None,
        其他字段(comparisons / runs)照常(对称 comparison 失败降级模式)。
        """
        ws = tmp_path / "with_skill" / "eval-1" / "run-1"
        _write_grading_run(ws, json.dumps(_grading_payload()), encoding="utf-8")
        _write_without_calibration_run(tmp_path)

        agg = BenchmarkAggregator()
        with (
            patch("sift.execution.benchmark.run_comparisons", return_value=[]),
            patch("sift.execution.benchmark.run_analysis", return_value=([], None)),
            patch(
                ("sift.execution.benchmark._collect_per_side_calibration"),
                side_effect=RuntimeError("calibration explode"),
            ),
        ):
            benchmark = agg.aggregate(tmp_path, tmp_path)

        assert benchmark.calibration_summary is None  # 降级,不崩
        assert len(benchmark.runs) == 2  # 其他字段照常


def _aggregate_fixture(iteration: Path, tmp_path: Path):
    """聚合本地测试产物，仅在本次调用中替换比较和分析的模型阶段。"""
    agg = BenchmarkAggregator()
    with (
        patch("sift.execution.benchmark.run_comparisons", return_value=[]),
        patch("sift.execution.benchmark.run_analysis", return_value=([], None)),
    ):
        benchmark = agg.aggregate(iteration, tmp_path)
    return benchmark


def make_comparison_run(base: Path, marker: str, expectations_text: str | None = None) -> None:
    (base / "outputs").mkdir(parents=True, exist_ok=True)
    (base / "outputs" / "out.txt").write_text(marker, encoding="utf-8")
    if expectations_text is not None:
        (base / "grading.json").write_text(
            json.dumps(_grading_payload(expectations=[{"text": expectations_text}], summary={})),
            encoding="utf-8",
        )


def _grading_payload(**changes):
    data = {
        "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
        "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
    }
    data.update(changes)
    return data


def _run_timing(**changes):
    data = {"total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0}
    data.update(changes)
    return data


def _grading_summary(**changes):
    data = {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0}
    data.update(changes)
    return data


def _expectation_result(**changes):
    data = {"text": "t", "passed": True, "evidence": "e"}
    data.update(changes)
    return data


def _write_grading_run(directory, grading_json, **write_options):
    directory.mkdir(parents=True)
    (directory / "outputs").mkdir()
    (directory / "grading.json").write_text(grading_json, **write_options)


def _comparison_skill(tmp_path, text):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


def _comparison_transcripts(tmp_path):
    (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
    (tmp_path / "with_skill" / "run-1" / "transcript.md").write_text("WS", encoding="utf-8")
    (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
    (tmp_path / "without_skill" / "run-1" / "transcript.md").write_text("WOS", encoding="utf-8")


def _write_without_calibration_run(tmp_path):
    wos = tmp_path / "without_skill" / "eval-1" / "run-1"
    _write_grading_run(
        wos,
        json.dumps(
            _grading_payload(
                expectations=[_expectation_result(passed=False)],
                summary=_grading_summary(passed=0, failed=1, pass_rate=0.0),
            )
        ),
        encoding="utf-8",
    )
