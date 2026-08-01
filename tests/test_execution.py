"""Tests for eval loader and execution engine."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skill_eval.evals.loader import EvalLoader
from skill_eval.errors import EvalValidationError
from skill_eval.execution.benchmark import BenchmarkAggregator, calculate_stats
import skill_eval.execution.comparison_suite as comparison_suite
from skill_eval.execution.comparison_suite import run_comparisons, _compare_with_retry
from skill_eval.models import ComparisonResult, EvalSet, StatsValue


class TestEvalLoader:
    def test_load_valid(self, sample_evals_path: Path):
        loader = EvalLoader()
        eval_set = loader.load(sample_evals_path)
        assert eval_set.skill_name == "sample-skill"
        assert len(eval_set.evals) == 2

    def test_load_missing_file(self, tmp_path: Path):
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="not found"):
            loader.load(tmp_path / "nope.json")

    def test_load_invalid_json(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text("not json{{{", encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="Invalid JSON"):
            loader.load(p)

    def test_load_missing_skill_name(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"evals": [{"id": 1, "prompt": "x"}]}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="skill_name"):
            loader.load(p)

    def test_load_empty_skill_name(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"skill_name": "", "evals": [{"id": 1, "prompt": "x"}]}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="non-empty"):
            loader.load(p)

    def test_load_empty_evals(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({"skill_name": "test", "evals": []}), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="non-empty"):
            loader.load(p)

    def test_load_duplicate_ids(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}, {"id": 1, "prompt": "b"}],
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="Duplicate"):
            loader.load(p)

    def test_load_empty_prompt(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": ""}],
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="prompt"):
            loader.load(p)

    def test_load_preserves_trigger_probes(self, tmp_path: Path):
        """loader 须透传 trigger_probes 段(否则 inspect/freeze 等走 loader 的路径看不到)。"""
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}],
            "trigger_probes": [
                {"id": 1, "prompt": "q1", "expect_trigger": True, "rationale": "r"},
                {"id": 2, "prompt": "q2", "expect_trigger": False, "rationale": ""},
            ],
        }), encoding="utf-8")
        eval_set = EvalLoader().load(p)
        assert len(eval_set.trigger_probes) == 2
        assert eval_set.trigger_probes[0].expect_trigger is True
        assert eval_set.trigger_probes[1].expect_trigger is False

    def test_load_trigger_probe_missing_field_rejected(self, tmp_path: Path):
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}],
            "trigger_probes": [{"id": 1, "prompt": "q"}],  # 缺 expect_trigger
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="expect_trigger"):
            loader.load(p)

    def test_load_trigger_probe_duplicate_ids(self, tmp_path: Path):
        """trigger_probes 重复 id 也要拦(镜像 evals 的 seen_ids;doc 称 id「唯一」)。"""
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}],
            "trigger_probes": [
                {"id": 1, "prompt": "q1", "expect_trigger": True},
                {"id": 1, "prompt": "q2", "expect_trigger": False},  # 重复 id
            ],
        }), encoding="utf-8")
        loader = EvalLoader()
        with pytest.raises(EvalValidationError, match="Duplicate trigger probe id"):
            loader.load(p)

    def test_load_no_trigger_probes_defaults_empty(self, tmp_path: Path):
        """旧 evals.json 无 trigger_probes 段 → [](向后兼容)。"""
        p = tmp_path / "evals.json"
        p.write_text(json.dumps({
            "skill_name": "test",
            "evals": [{"id": 1, "prompt": "a"}],
        }), encoding="utf-8")
        assert EvalLoader().load(p).trigger_probes == []


class TestCalculateStats:
    def test_empty(self):
        s = calculate_stats([])
        assert s.mean == 0.0

    def test_single(self):
        s = calculate_stats([0.5])
        assert s.mean == 0.5
        assert s.stddev == 0.0

    def test_multiple(self):
        s = calculate_stats([1.0, 2.0, 3.0])
        assert s.mean == 2.0
        assert s.stddev > 0


class TestBenchmarkAggregator:
    def test_aggregate_from_iteration(self, tmp_path: Path):
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下

        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            config_dir.mkdir(parents=True)
            (config_dir / "outputs").mkdir()
            (config_dir / "grading.json").write_text(json.dumps({
                "expectations": [{"text": "t", "passed": config == "with_skill", "evidence": "e"}],
                "summary": {"passed": 1 if config == "with_skill" else 0, "failed": 0 if config == "with_skill" else 1, "total": 1, "pass_rate": 1.0 if config == "with_skill" else 0.0},
            }), encoding="utf-8")
            (config_dir / "timing.json").write_text(json.dumps({
                "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
            }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        assert len(benchmark.runs) == 2
        assert benchmark.run_summary["delta"]["pass_rate"] == "+1.00"

    def test_real_model_from_transcript(self, tmp_path):
        """从 transcript.jsonl 的 result event.modelUsage key 抓真实模型名。"""
        from skill_eval.execution.benchmark import _real_model_from_transcript
        rd = tmp_path / "run-1"; rd.mkdir()
        (rd / "transcript.jsonl").write_text(
            '{"type":"assistant","message":{"model":"x"}}\n'
            '{"type":"result","modelUsage":{"glm-5.2[1m]":{"inputTokens":100}}}\n',
            encoding="utf-8")
        assert _real_model_from_transcript(rd) == "glm-5.2[1m]"
        assert _real_model_from_transcript(tmp_path / "nope") == ""  # 无 transcript
        rd2 = tmp_path / "run-2"; rd2.mkdir()
        (rd2 / "transcript.jsonl").write_text('{"type":"assistant","message":{"model":"x"}}', encoding="utf-8")
        assert _real_model_from_transcript(rd2) == ""  # 无 result event

    def test_aggregate_fills_executor_model_from_transcript(self, tmp_path: Path):
        """aggregate 从 transcript 抓真实模型名填 metadata.executor_model。
        comparator/analyzer 同源复用(真实 run 验证,单测环境 comparison/analysis 跳过)。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        config_dir = iteration / "with_skill" / "run-1"
        config_dir.mkdir(parents=True)
        (config_dir / "outputs").mkdir()
        (config_dir / "grading.json").write_text(json.dumps({
            "expectations": [], "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
        }), encoding="utf-8")
        (config_dir / "transcript.jsonl").write_text(
            '{"type":"result","modelUsage":{"glm-5.2[1m]":{"inputTokens":100}}}',
            encoding="utf-8")
        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)
        assert benchmark.metadata.executor_model == "glm-5.2[1m]"

    def test_aggregate_evals_run_from_runs_not_single_checkpoints(self, tmp_path: Path):
        """evals_run 用 runs(所有已聚合 eval),不从单个 checkpoints.json 推。

        回归:_load_checkpoints 只读**首个** checkpoints.json(单 run、只见自己 eval),
        旧 `eval_ids = ... checkpoints_data if checkpoints_data else runs` 会短路到 checkpoints
        → evals_run 只含那个 run 的 eval(如 [1]),漏掉其余 eval。真实多 eval run 应得完整列表。
        """
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        config = iteration / "with_skill"
        for eval_id in (1, 2, 3):
            rd = config / f"eval-{eval_id}" / "run-1"
            rd.mkdir(parents=True)
            (rd / "outputs").mkdir()
            (rd / "grading.json").write_text(json.dumps({
                "expectations": [], "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
            }), encoding="utf-8")
        # 首个 run 落一个只含 eval_id=1 的 checkpoints.json → 触发旧 checkpoints 短路分支
        (config / "eval-1" / "run-1" / "checkpoints.json").write_text(
            json.dumps([{"eval_id": 1, "step": "x"}]), encoding="utf-8")
        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)
        assert benchmark.metadata.evals_run == [1, 2, 3]

    def test_aggregate_dual_rate_delta(self, tmp_path: Path):
        """双率:grading summary 含 pass_rate_programmatic/_llm → run_summary + delta 双率。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        summaries = {
            "with_skill": {"passed": 2, "failed": 1, "total": 3, "pass_rate": 2 / 3,
                           "pass_rate_programmatic": 1.0, "pass_rate_llm": 0.5},
            "without_skill": {"passed": 1, "failed": 2, "total": 3, "pass_rate": 1 / 3,
                              "pass_rate_programmatic": 0.5, "pass_rate_llm": 0.0},
        }
        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            config_dir.mkdir(parents=True)
            (config_dir / "outputs").mkdir()
            (config_dir / "grading.json").write_text(json.dumps({
                "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
                "summary": summaries[config],
            }), encoding="utf-8")
            (config_dir / "timing.json").write_text(json.dumps({
                "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
            }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        ws = benchmark.run_summary["with_skill"]
        assert ws["pass_rate_programmatic"]["mean"] == 1.0
        assert ws["pass_rate_llm"]["mean"] == 0.5
        d = benchmark.run_summary["delta"]
        assert d["pass_rate_programmatic"] == "+0.50"  # with 1.0 - without 0.5
        assert d["pass_rate_llm"] == "+0.50"  # with 0.5 - without 0.0

    def test_aggregate_runs_carry_dual_rate_per_run(self, tmp_path: Path):
        """per-(eval,run) 双率透到 runs[].result(不只 run_summary 聚合):
        检查点总览 + 逐项表的 3-dim 列要 per-eval per-run 可见。
        RED:旧 runs[].result 只有 overall pass_rate;split 只进 entry_data→configs_data(Δ 卡)。"""
        iteration = tmp_path
        config_dir = iteration / "with_skill" / "eval-1" / "run-1"
        config_dir.mkdir(parents=True)
        (config_dir / "outputs").mkdir()
        (config_dir / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
            "summary": {"passed": 2, "failed": 1, "total": 3, "pass_rate": 2 / 3,
                        "pass_rate_programmatic": 1.0, "pass_rate_llm": 0.5},
        }), encoding="utf-8")
        (config_dir / "timing.json").write_text(json.dumps({
            "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
        }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        r = benchmark.runs[0].result
        assert abs(r.pass_rate_programmatic - 1.0) < 1e-9
        assert abs(r.pass_rate_llm - 0.5) < 1e-9

    def test_aggregate_eval_name_from_run_debug(self, tmp_path: Path):
        """eval_name 从 run_debug.json 取真名(如 happy_path_basic_categorization),
        不再用 skill 名。检查点总览 + 逐项表靠它显题名,否则全显 "Eval N"。
        RED:旧代码 eval_name=name(skill 名,parse_skill_md 来),run_debug 没读。"""
        iteration = tmp_path
        run_dir = iteration / "with_skill" / "eval-1" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "outputs").mkdir()
        (run_dir / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
            "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
        }), encoding="utf-8")
        (run_dir / "timing.json").write_text(json.dumps({
            "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
        }), encoding="utf-8")
        (run_dir / "run_debug.json").write_text(json.dumps({
            "eval_name": "happy_path_basic_categorization", "eval_id": 1,
        }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        assert benchmark.runs[0].eval_name == "happy_path_basic_categorization"

    def test_aggregate_eval_name_empty_when_no_run_debug(self, tmp_path: Path):
        """无 run_debug.json → eval_name 空(viewer fallback "Eval {id}"),不用 skill 名顶替。"""
        iteration = tmp_path
        run_dir = iteration / "with_skill" / "eval-1" / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "outputs").mkdir()
        (run_dir / "grading.json").write_text(json.dumps({
            "expectations": [],
            "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
        }), encoding="utf-8")
        (run_dir / "timing.json").write_text(json.dumps({
            "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
        }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        assert benchmark.runs[0].eval_name == ""

    def test_run_comparison_analysis_handles_semantic_winner(self, tmp_path: Path):
        """run_comparison_analysis 认语义 winner(with/without),不只老 A/B。
        RED:comparator 存语义 winner(position 随机化 + _translate 后),旧代码只判 ==A/==B
        → 恒 None → 深度分析永远不显示。改后 winner='with' 正常往下走到 analyze_comparison。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# body\n", encoding="utf-8")
        ws = tmp_path / "with_skill" / "run-1"; ws.mkdir(parents=True)
        (ws / "transcript.md").write_text("ws", encoding="utf-8")
        wos = tmp_path / "without_skill" / "run-1"; wos.mkdir(parents=True)
        (wos / "transcript.md").write_text("wos", encoding="utf-8")

        benchmark = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with", reasoning="r")],
        )
        sentinel = object()
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: sentinel})()

        result = run_comparison_analysis(None, tmp_path, benchmark, analyzer)
        assert result is sentinel   # 旧代码这里 None(winner 'with' 不是 A/B)

    def test_run_comparison_analysis_semantic_winner_mapping(self, tmp_path: Path):
        """语义 winner 正确映射 winner/loser transcript:with→ws 赢、without→wos 赢。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "with_skill" / "run-1" / "transcript.md").write_text("WS", encoding="utf-8")
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1" / "transcript.md").write_text("WOS", encoding="utf-8")

        captured: dict = {}
        def fake(**kw):
            captured.update(kw)
            return "OK"
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: fake(**kw)})()

        bd = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
                           comparisons=[ComparisonResult(winner="with", reasoning="r")])
        run_comparison_analysis(None, tmp_path, bd, analyzer)
        assert captured["winner_transcript"] == "WS"   # with 赢 → winner 是 ws

        captured.clear()
        bd2 = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
                            comparisons=[ComparisonResult(winner="without", reasoning="r")])
        run_comparison_analysis(None, tmp_path, bd2, analyzer)
        assert captured["winner_transcript"] == "WOS"  # without 赢 → winner 是 wos

    def test_run_comparison_analysis_winner_by_overall_majority(self, tmp_path: Path):
        """winner 不再取 comparisons[0],而是**整体多数票**(排除 failed/tie)。
        场景:comparisons[0] 是 without 赢(与整体反),但整体 with 多 → winner 应=with,
        代表判词也取 with 侧赢的、不是 comparisons[0] 的 without 判词。
        RED:旧代码 winner=comparisons[0].winner → 'without'(反整体)。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "with_skill" / "run-1" / "transcript.md").write_text("WS", encoding="utf-8")
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1" / "transcript.md").write_text("WOS", encoding="utf-8")

        captured: dict = {}
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: (captured.update(kw) or "OK")})()

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[
                ComparisonResult(winner="without", reasoning="comparisons[0] 是 without 赢"),  # 反整体
                ComparisonResult(winner="with", reasoning="with 赢 case A"),
                ComparisonResult(winner="with", reasoning="with 赢 case B"),
                ComparisonResult(winner="with", reasoning="with 赢 case C"),
            ],  # with 3 > without 1
        )
        run_comparison_analysis(None, tmp_path, bd, analyzer)
        assert captured["winner"] == "with"  # 整体多数票,不是 comparisons[0] 的 without
        assert captured["winner_transcript"] == "WS"
        # 代表判词来自 with 侧赢的(不是 comparisons[0] 的 without 判词)
        assert "comparisons[0]" not in captured["comparison_reasoning"]
        assert "with 赢 case" in captured["comparison_reasoning"]
        # 整体摘要
        assert "[整体分布]" in captured["comparison_reasoning"]
        assert "3" in captured["comparison_reasoning"]   # with 3 次
        assert "1" in captured["comparison_reasoning"]   # without 1 次

    def test_run_comparison_analysis_focus_skill_is_tested_skill(self, tmp_path: Path):
        """focus_skill 永远 = 被测 skill(skill_content),不随 winner 侧变化:
        winner=with 时被测 skill 在 winner 位,winner=without 时在 loser 位,
        但 focus_skill 两场景都该 = 被测 skill 全文(改进对象恒定)。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# 被测 skill body\n", encoding="utf-8")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "with_skill" / "run-1" / "transcript.md").write_text("WS", encoding="utf-8")
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1" / "transcript.md").write_text("WOS", encoding="utf-8")

        captured: dict = {}
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: (captured.update(kw) or "OK")})()

        # winner=with(被测 skill 在 winner 位)
        bd_with = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
                                comparisons=[ComparisonResult(winner="with", reasoning="r")])
        run_comparison_analysis(None, tmp_path, bd_with, analyzer)
        assert "被测 skill body" in captured["focus_skill"]

        captured.clear()
        # winner=without(被测 skill 在 loser 位)
        bd_without = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
                                   comparisons=[ComparisonResult(winner="without", reasoning="r")])
        run_comparison_analysis(None, tmp_path, bd_without, analyzer)
        assert "被测 skill body" in captured["focus_skill"]   # 仍是被测 skill,不是 "(no skill)"

    def test_run_comparison_analysis_tie_returns_none(self, tmp_path: Path):
        """整体平局(with==without)→ 无代表性强弱,返 None 不分析。
        RED:旧代码 winner=comparisons[0].winner='with' → 不 None 继续分析。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="with"), ComparisonResult(winner="without")],  # 平局
        )
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: "OK"})()
        assert run_comparison_analysis(None, tmp_path, bd, analyzer) is None

    def test_run_comparison_analysis_no_valid_returns_none(self, tmp_path: Path):
        """全 tie/failed → 无有效胜负,返 None(回归保护)。"""
        from skill_eval.execution.analysis_suite import run_comparison_analysis
        from skill_eval.models import BenchmarkData, BenchmarkMetadata, ComparisonResult

        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)

        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="s", skill_path=str(skill_dir)),
            comparisons=[ComparisonResult(winner="tie"), ComparisonResult(winner="tie", failed=True)],
        )
        analyzer = type("A", (), {"analyze_comparison": lambda self, **kw: "OK"})()
        assert run_comparison_analysis(None, tmp_path, bd, analyzer) is None

    def test_aggregate_dual_rate_null_when_class_missing(self, tmp_path: Path):
        """某类 expectation 全无(pass_rate_xx 缺)→ 该双率 stats None、delta 该项 None。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下
        for config in ("with_skill", "without_skill"):
            config_dir = iteration / config / "run-1"
            config_dir.mkdir(parents=True)
            (config_dir / "outputs").mkdir()
            (config_dir / "grading.json").write_text(json.dumps({
                "expectations": [{"text": "t", "passed": True, "evidence": "e", "source": "programmatic"}],
                "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0,
                            "pass_rate_programmatic": 1.0},  # 无 pass_rate_llm
            }), encoding="utf-8")
            (config_dir / "timing.json").write_text(json.dumps({
                "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
            }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        ws = benchmark.run_summary["with_skill"]
        assert ws["pass_rate_programmatic"]["mean"] == 1.0
        assert ws["pass_rate_llm"] is None  # 无 llm expectation
        assert benchmark.run_summary["delta"]["pass_rate_llm"] is None

    def test_aggregate_ignores_superseded_retry_base(self, tmp_path: Path):
        """benchmark 聚合 retry-glob bug 回归:被 retry 取代的失败 base 不当独立 run 计入。
        with eval-1 有 run-1(base 0.0)+ run-1-retry-1(最终 0.75),聚合应只算 retry-1。
        旧 glob("eval-*/run-*") 两条都读 → with 均值被失败 base 拖低 → delta 失真。"""
        iteration = tmp_path  # iteration-1 死层已删:run 树根即 output_dir,config 目录直接落其下

        def make(run_dir: Path, pass_rate: float) -> None:
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "outputs").mkdir(exist_ok=True)
            (run_dir / "grading.json").write_text(json.dumps({
                "expectations": [{"text": "t", "passed": pass_rate >= 0.5, "evidence": "e"}],
                "summary": {"passed": 0, "failed": 1, "total": 1, "pass_rate": pass_rate,
                            "pass_rate_llm": pass_rate},
            }), encoding="utf-8")
            (run_dir / "timing.json").write_text(json.dumps({
                "total_tokens": 100, "duration_ms": 5000, "total_duration_seconds": 5.0,
            }), encoding="utf-8")

        # with:eval-1 run-1 base(失败)+ run-1-retry-1(最终);eval-2 run-1
        make(iteration / "with_skill" / "eval-1" / "run-1", 0.0)
        make(iteration / "with_skill" / "eval-1" / "run-1-retry-1", 0.75)
        make(iteration / "with_skill" / "eval-2" / "run-1", 1.0)
        # without:eval-1/eval-2 各 run-1(无 retry)
        make(iteration / "without_skill" / "eval-1" / "run-1", 0.25)
        make(iteration / "without_skill" / "eval-2" / "run-1", 0.5)

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration, tmp_path)

        # with 只 2 条最终尝试(eval-1/run-1→retry 0.75 + eval-2/run-1 1.0),非 3 条
        assert len(benchmark.runs) == 4  # with 2 + without 2,不含被取代的 base
        assert benchmark.run_summary["with_skill"]["pass_rate"]["mean"] == pytest.approx((0.75 + 1.0) / 2)
        # delta 用最终尝试:with mean 0.875 - without mean 0.375 = +0.50
        assert benchmark.run_summary["delta"]["pass_rate"] == "+0.50"

    def test_aggregate_empty(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        iteration.mkdir()

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(iteration)

        assert benchmark.runs == []

    def test_aggregate_survives_comparison_failure(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "expectations": [{"text": "x", "passed": True, "evidence": ""}],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.0, "passed": 0, "failed": 2, "total": 2},
            "expectations": [{"text": "x", "passed": False, "evidence": ""}],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", side_effect=RuntimeError("LLM timeout")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert "with_skill" in benchmark.run_summary
        assert benchmark.comparisons is None or benchmark.comparisons == []

    def test_aggregate_survives_analysis_failure(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 1.0, "passed": 2, "failed": 0, "total": 2},
            "expectations": [],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.5, "passed": 1, "failed": 1, "total": 2},
            "expectations": [],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", side_effect=RuntimeError("LLM rate limited")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert isinstance(benchmark.run_summary, dict)
        assert benchmark.analysis is None

    def test_aggregate_survives_both_failures(self, tmp_path: Path):
        iteration = tmp_path / "iteration-1"
        with_skill_dir = iteration / "with_skill" / "eval-1" / "run-1"
        with_skill_dir.mkdir(parents=True)
        (with_skill_dir / "outputs").mkdir()
        (with_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.8, "passed": 4, "failed": 1, "total": 5},
            "expectations": [],
        }))

        without_skill_dir = iteration / "without_skill" / "eval-1" / "run-1"
        without_skill_dir.mkdir(parents=True)
        (without_skill_dir / "outputs").mkdir()
        (without_skill_dir / "grading.json").write_text(json.dumps({
            "summary": {"pass_rate": 0.2, "passed": 1, "failed": 4, "total": 5},
            "expectations": [],
        }))

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", side_effect=RuntimeError("fail1")), \
             patch("skill_eval.execution.benchmark.run_analysis", side_effect=RuntimeError("fail2")):
            benchmark = agg.aggregate(iteration)

        assert len(benchmark.runs) == 2
        assert benchmark.comparisons is None or benchmark.comparisons == []
        assert benchmark.analysis is None


class TestComparisonSkipWhenProgrammaticOnly:
    """④ 盲比较 programmatic-only 跳过:enable_llm=False 时 run_comparisons 直接返回 []。"""

    def test_skips_when_enable_llm_false(self, tmp_path: Path):
        # with/without run 目录齐备(若无门禁会进入比较流程)
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        agg = BenchmarkAggregator(model="m", enable_llm=False)
        with patch("skill_eval.execution.comparison_suite.BlindComparator") as mock_cmp:
            assert run_comparisons(agg, tmp_path) == []
        mock_cmp.assert_not_called()  # 门禁在 BlindComparator 实例化之前

    def test_proceeds_when_enable_llm_true(self, tmp_path: Path):
        (tmp_path / "with_skill" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "run-1").mkdir(parents=True)
        agg = BenchmarkAggregator(model="m", enable_llm=True)
        with patch("skill_eval.execution.comparison_suite.BlindComparator") as mock_cmp:
            mock_cmp.return_value.compare.return_value = ComparisonResult(winner="with", reasoning="t")
            result = run_comparisons(agg, tmp_path)
        assert mock_cmp.called  # 门禁放行,BlindComparator 被实例化
        assert len(result) == 1


class TestComparisonPairing:
    """配对 bug 回归:run_comparisons 必须按 (eval, run) 显式配对、
    with 侧有 retry 目录时取最终 retry,不能 glob+zip 错位(原 bug:retry 目录混入
    glob → 排序后 zip 把 with 多出的 retry 配给 without 的下一个 eval → 跨 eval 伪 tie)。"""

    @staticmethod
    def _make_run(base: Path, marker: str, expectations_text: str | None = None) -> None:
        (base / "outputs").mkdir(parents=True, exist_ok=True)
        (base / "outputs" / "out.txt").write_text(marker, encoding="utf-8")
        if expectations_text is not None:
            (base / "grading.json").write_text(
                json.dumps({"expectations": [{"text": expectations_text}], "summary": {}}),
                encoding="utf-8",
            )

    @staticmethod
    def _fake_comparator_factory(calls: list, markers: tuple[str, ...]):
        class _FakeComparator:
            def __init__(self, *a, **kw):
                pass

            def compare(self, output_a, output_b, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
                calls.append((output_a, output_b))
                return ComparisonResult(winner="with", reasoning="ok")

        def _tok(s: str) -> str:
            return next(m for m in markers if m in s)

        return _FakeComparator, _tok

    def test_pairs_by_eval_and_run_taking_final_retry(self, tmp_path: Path, monkeypatch):
        """with 侧 eval-1 有 run-1(base)+ run-1-retry-1(最终应取 retry-1)。
        旧 glob+zip:ws_runs=[e1/r1, e1/r1-retry1, e2/r1] 配 wos_runs=[e1/r1, e2/r1]
        → 错位成 [(base, wos-e1), (retry, wos-e2)] 跨 eval。修复后按 (eval,run) 对齐。"""
        markers = ("W-E1-R1-BASE", "W-E1-R1-RETRY", "W-E2-R1", "WO-E1-R1", "WO-E2-R1")
        calls: list[tuple[str, str]] = []
        fake_cmp, _tok = self._fake_comparator_factory(calls, markers)
        monkeypatch.setattr(comparison_suite, "BlindComparator", fake_cmp)

        # with:eval-1 双层(base + retry)、eval-2 单层
        self._make_run(tmp_path / "with_skill" / "eval-1" / "run-1", "W-E1-R1-BASE")
        self._make_run(tmp_path / "with_skill" / "eval-1" / "run-1-retry-1", "W-E1-R1-RETRY")
        self._make_run(tmp_path / "with_skill" / "eval-2" / "run-1", "W-E2-R1")
        # without:无 retry
        self._make_run(tmp_path / "without_skill" / "eval-1" / "run-1", "WO-E1-R1")
        self._make_run(tmp_path / "without_skill" / "eval-2" / "run-1", "WO-E2-R1")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        results = run_comparisons(_Agg(), tmp_path)
        assert len(results) == 2  # 只 (eval-1,run-1) + (eval-2,run-1) 两对

        pairings = sorted((_tok(w), _tok(wo)) for w, wo in calls)
        assert pairings == [
            ("W-E1-R1-RETRY", "WO-E1-R1"),  # eval-1/run-1:with 取最终 retry-1,非 base
            ("W-E2-R1", "WO-E2-R1"),        # eval-2/run-1:各自对齐
        ]
        all_with = [_tok(c[0]) for c in calls]
        assert "W-E1-R1-BASE" not in all_with  # retry 覆盖 base,base 不进比较

    def test_pairs_flat_structure_no_retry(self, tmp_path: Path, monkeypatch):
        """flat 结构(with_skill/run-N,无 eval 分层)配对不回归。"""
        markers = ("W-R1", "W-R2", "WO-R1", "WO-R2")
        calls: list[tuple[str, str]] = []
        fake_cmp, _tok = self._fake_comparator_factory(calls, markers)
        monkeypatch.setattr(comparison_suite, "BlindComparator", fake_cmp)

        self._make_run(tmp_path / "with_skill" / "run-1", "W-R1")
        self._make_run(tmp_path / "with_skill" / "run-2", "W-R2")
        self._make_run(tmp_path / "without_skill" / "run-1", "WO-R1")
        self._make_run(tmp_path / "without_skill" / "run-2", "WO-R2")

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

            def compare(self, output_a, output_b, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
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
        assert results[0].winner == "v1"          # winner 用 v1 label,非 with
        assert seen_labels == [("v1", "v2")]      # comp_label 透到 comparator

    def test_default_sides_none_falls_back_to_with_without(self, tmp_path: Path, monkeypatch):
        """sides=None → 默认 with_skill/without_skill dir + with/without label(向后兼容)。"""
        class _FakeComparator:
            def __init__(self, *a, **kw):
                pass

            def compare(self, output_a, output_b, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
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
        assert results[0].winner == "with"           # 默认 label


class TestComparisonRetry:
    """_compare_with_retry 重试机制:compare() 返回 failed=True(调用/解析失败)时重试。
    旧实现 try/except 重试,但 compare() 把失败吞成 TIE 永不抛 → 重试循环是死代码。"""

    def test_retries_until_success(self):
        """前 2 次 failed、第 3 次成功 → 重试到成功即返回。"""
        attempts = []

        class _Flaky:
            def compare(self, output_a=None, output_b=None, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
                attempts.append(len(attempts) + 1)
                if len(attempts) < 3:
                    return ComparisonResult(winner="tie", reasoning="无法解析", failed=True)
                return ComparisonResult(winner="with", reasoning="ok", failed=False)

        with patch.object(comparison_suite, "time"):  # 跳过 backoff sleep
            result = _compare_with_retry(_Flaky(), "ws", "wos", "ep", None, max_retries=3)
        assert result.winner == "with"
        assert len(attempts) == 3  # 重试了 2 次

    def test_exhausts_to_none_when_all_fail(self):
        """每次都 failed → 耗尽 max_retries 返回 None(由调用方降级 tie)。"""
        attempts = []

        class _AlwaysFail:
            def compare(self, output_a=None, output_b=None, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
                attempts.append(1)
                return ComparisonResult(winner="tie", reasoning="对比调用失败", failed=True)

        with patch.object(comparison_suite, "time"):
            result = _compare_with_retry(_AlwaysFail(), "ws", "wos", "ep", None, max_retries=3)
        assert result is None
        assert len(attempts) == 3

    def test_no_retry_on_success(self):
        """成功(failed=False)→ 立即返回,不重试。"""
        attempts = []

        class _Ok:
            def compare(self, output_a=None, output_b=None, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
                attempts.append(1)
                return ComparisonResult(winner="with", reasoning="ok", failed=False)

        result = _compare_with_retry(_Ok(), "ws", "wos", "ep", None, max_retries=3)
        assert result.winner == "with"
        assert len(attempts) == 1

    def test_exhausted_degradation_marked_failed(self, tmp_path: Path, monkeypatch):
        """compare 恒 failed → _compare_with_retry 耗尽 None → run_comparisons 降级 tie 必带
        failed=True(否则失败混进 genuine-tie 桶,污染 win-rate 分母)。复用 TestComparisonPairing
        的 _make_run + run_comparisons 范式。"""
        class _AlwaysFails:
            def __init__(self, *a, **kw):
                pass

            def compare(self, output_a=None, output_b=None, eval_prompt=None, expectations=None, label_a=None, label_b=None, **kwargs):
                return ComparisonResult(winner="tie", reasoning="对比调用失败", failed=True)

        monkeypatch.setattr(comparison_suite, "BlindComparator", _AlwaysFails)
        TestComparisonPairing._make_run(tmp_path / "with_skill" / "run-1", "w")
        TestComparisonPairing._make_run(tmp_path / "without_skill" / "run-1", "wo")

        class _Agg:
            enable_llm = True
            model = None
            timeout = 10

        with patch.object(comparison_suite, "time"):  # 跳 backoff sleep
            results = run_comparisons(_Agg(), tmp_path)
        assert len(results) == 1
        assert results[0].failed is True  # 修复前为 False


class TestComparisonSummary:
    """ComparisonSummary.from_comparisons:win-rate 有效分母 = with+without+genuine tie,
    failed 排除(失败≠打平)。"""

    def test_mixed_win_rate_excludes_failed(self):
        comps = [
            ComparisonResult(winner="with", reasoning=""),
            ComparisonResult(winner="without", reasoning=""),
            ComparisonResult(winner="tie", reasoning="等价"),              # genuine tie
            ComparisonResult(winner="tie", reasoning="耗尽", failed=True),  # 失败
        ]
        from skill_eval.models import ComparisonSummary
        s = ComparisonSummary.from_comparisons(comps)
        assert s.total == 4
        assert (s.a_wins, s.b_wins, s.ties, s.failed) == (1, 1, 1, 1)
        assert s.valid == 3            # genuine tie 计入,failed 排除
        assert s.win_rate == 1 / 3
        assert s.label_a == "with" and s.label_b == "without"  # 默认 label

    def test_genuine_tie_counts_in_denominator(self):
        # 2 with / 0 without / 3 tie → win_rate 2/5(打平算一场比过)
        comps = [ComparisonResult(winner="with", reasoning="")] * 2 \
            + [ComparisonResult(winner="tie", reasoning="")] * 3
        from skill_eval.models import ComparisonSummary
        s = ComparisonSummary.from_comparisons(comps)
        assert s.win_rate == 2 / 5
        assert s.valid == 5

    def test_all_failed_win_rate_none(self):
        comps = [ComparisonResult(winner="tie", reasoning="x", failed=True) for _ in range(3)]
        from skill_eval.models import ComparisonSummary
        s = ComparisonSummary.from_comparisons(comps)
        assert s.valid == 0
        assert s.win_rate is None      # 不报假 0%
        assert s.failed == 3

    def test_v1v2_label_bucketing(self):
        """v1/v2 模式:label_a/b 驱动分桶;win_rate 恒为 side A(v1)胜率。"""
        comps = [
            ComparisonResult(winner="v1", reasoning=""),
            ComparisonResult(winner="v2", reasoning=""),
            ComparisonResult(winner="v1", reasoning=""),
            ComparisonResult(winner="tie", reasoning="等价"),
        ]
        from skill_eval.models import ComparisonSummary
        s = ComparisonSummary.from_comparisons(comps, label_a="v1", label_b="v2")
        assert (s.a_wins, s.b_wins, s.ties, s.failed) == (2, 1, 1, 0)
        assert s.valid == 4
        assert s.win_rate == 0.5       # a_wins(2)/valid(4)
        assert s.label_a == "v1" and s.label_b == "v2"

    def test_empty_returns_none(self):
        from skill_eval.models import ComparisonSummary
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
        from skill_eval.models import CalibrationReport
        from skill_eval.models.enums import CalibrationVerdict, ProcessStatus
        result_pass = verdict in (CalibrationVerdict.TRUSTED, CalibrationVerdict.HIGH_SCORE_SUSPECT)
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
        断言:with_skill.total==2、high_score_suspects 长度 1、without_skill is None。"""
        from skill_eval.execution.benchmark import _collect_per_side_calibration
        from skill_eval.models import CalibrationSummary, HardViolation
        from skill_eval.models.enums import CalibrationVerdict

        hv = HardViolation(
            category="prohibition", instruction_text="禁止编造错误码", evidence="E-FAKE-999",
        )
        # with_skill: 两份 calibration.json(一 TRUSTED、一 HIGH_SCORE_SUSPECT 带 hv)
        ws1 = tmp_path / "with_skill" / "eval-1" / "run-1"
        ws1.mkdir(parents=True)
        (ws1 / "calibration.json").write_text(
            self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        ws2 = tmp_path / "with_skill" / "eval-1" / "run-2"
        ws2.mkdir(parents=True)
        (ws2 / "calibration.json").write_text(
            self._calibration_json(
                CalibrationVerdict.HIGH_SCORE_SUSPECT,
                hard_violations=[hv], compliance_rate=0.5,
            ), encoding="utf-8")
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
        被 retry 取代的失败 base 不当独立 run 计入(与 aggregate / run_comparisons 同源)。"""
        from skill_eval.execution.benchmark import _collect_per_side_calibration
        from skill_eval.models.enums import CalibrationVerdict

        # eval-1 有 run-1(base TRUSTED)+ run-1-retry-1(最终 HIGH_SCORE_SUSPECT)
        base = tmp_path / "with_skill" / "eval-1" / "run-1"
        base.mkdir(parents=True)
        (base / "calibration.json").write_text(
            self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        retry = tmp_path / "with_skill" / "eval-1" / "run-1-retry-1"
        retry.mkdir(parents=True)
        (retry / "calibration.json").write_text(
            self._calibration_json(CalibrationVerdict.HIGH_SCORE_SUSPECT,
                                   hard_violations=[{"category": "prohibition",
                                                     "instruction_text": "x", "evidence": "y"}]),
            encoding="utf-8")

        sides = [("with_skill", None, "with")]
        per_side = _collect_per_side_calibration(tmp_path, sides)
        ws = per_side["with"]
        assert ws is not None and len(ws) == 1, \
            "base+retry 只算最终 retry 一条,不当两条独立 run"
        # 取的是 retry-1(HIGH_SCORE_SUSPECT),不是 base(TRUSTED)
        _eid, _rn, rep = ws[0]
        assert rep.verdict == CalibrationVerdict.HIGH_SCORE_SUSPECT

    def test_collect_per_side_skips_corrupt_json(self, tmp_path: Path):
        """calibration.json 损坏(非合法 JSON / schema 不符)→ 静默跳过,不抛。
        与 aggregate 主循环同款防御(grading.json 解析失败也跳过)。"""
        from skill_eval.execution.benchmark import _collect_per_side_calibration

        good = tmp_path / "with_skill" / "eval-1" / "run-1"
        good.mkdir(parents=True)
        (good / "calibration.json").write_text(
            self._calibration_json(__import__("skill_eval.models.enums", fromlist=["CalibrationVerdict"]).CalibrationVerdict.TRUSTED),
            encoding="utf-8")
        bad = tmp_path / "with_skill" / "eval-1" / "run-2"
        bad.mkdir(parents=True)
        (bad / "calibration.json").write_text("NOT JSON {{{", encoding="utf-8")

        per_side = _collect_per_side_calibration(tmp_path, [("with_skill", None, "with")])
        ws = per_side["with"]
        assert ws is not None and len(ws) == 1, "corrupt 那条跳过,只留 good 一条"

    def test_aggregate_sets_calibration_summary(self, tmp_path: Path):
        """端到端 sanity:aggregate() 把 calibration_summary 钉到 BenchmarkData。
        与 comparison_summary 对称(顶层 summary 字段)。"""
        from skill_eval.models import CalibrationSummary
        from skill_eval.models.enums import CalibrationVerdict

        # 造最小 run 树(让 aggregate 不崩)+ calibration.json
        ws = tmp_path / "with_skill" / "eval-1" / "run-1"
        ws.mkdir(parents=True)
        (ws / "outputs").mkdir()
        (ws / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
            "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
        }), encoding="utf-8")
        (ws / "calibration.json").write_text(
            self._calibration_json(CalibrationVerdict.TRUSTED), encoding="utf-8")
        wos = tmp_path / "without_skill" / "eval-1" / "run-1"
        wos.mkdir(parents=True)
        (wos / "outputs").mkdir()
        (wos / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": False, "evidence": "e"}],
            "summary": {"passed": 0, "failed": 1, "total": 1, "pass_rate": 0.0},
        }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)):
            benchmark = agg.aggregate(tmp_path, tmp_path)

        assert benchmark.calibration_summary is not None
        assert isinstance(benchmark.calibration_summary, CalibrationSummary)
        ws_side = benchmark.calibration_summary.sides.get("with")
        assert ws_side is not None and ws_side.total == 1
        assert ws_side.verdict_counts == {"TRUSTED": 1}
        # without_skill 侧无 calibration.json → None
        assert benchmark.calibration_summary.sides.get("without") is None

    def test_aggregate_survives_calibration_failure(self, tmp_path: Path):
        """calibration 收集抛异常 → 不崩 aggregate,calibration_summary 留 None,
        其他字段(comparisons / runs)照常(对称 comparison 失败降级模式)。"""
        ws = tmp_path / "with_skill" / "eval-1" / "run-1"
        ws.mkdir(parents=True)
        (ws / "outputs").mkdir()
        (ws / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": True, "evidence": "e"}],
            "summary": {"passed": 1, "failed": 0, "total": 1, "pass_rate": 1.0},
        }), encoding="utf-8")
        wos = tmp_path / "without_skill" / "eval-1" / "run-1"
        wos.mkdir(parents=True)
        (wos / "outputs").mkdir()
        (wos / "grading.json").write_text(json.dumps({
            "expectations": [{"text": "t", "passed": False, "evidence": "e"}],
            "summary": {"passed": 0, "failed": 1, "total": 1, "pass_rate": 0.0},
        }), encoding="utf-8")

        agg = BenchmarkAggregator()
        with patch("skill_eval.execution.benchmark.run_comparisons", return_value=[]), \
             patch("skill_eval.execution.benchmark.run_analysis", return_value=([], None)), \
             patch("skill_eval.execution.benchmark._collect_per_side_calibration",
                   side_effect=RuntimeError("calibration explode")):
            benchmark = agg.aggregate(tmp_path, tmp_path)

        assert benchmark.calibration_summary is None  # 降级,不崩
        assert len(benchmark.runs) == 2  # 其他字段照常


class TestCopySkillReferences:
    """_copy_skill_references:把 skill 的 references/ 拷进考场(P0-2 核心逻辑)."""

    @staticmethod
    def _make_skill(tmp_path: Path, with_refs: bool = True) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        if with_refs:
            refs = skill / "references"
            refs.mkdir()
            (refs / "guide.md").write_text("domain knowledge", encoding="utf-8")
        return skill

    def test_copies_references_into_work_dir(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=True)
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(skill, work)
        assert (work / "references" / "guide.md").read_text(encoding="utf-8") == "domain knowledge"

    def test_no_references_dir_is_noop(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=False)
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(skill, work)
        assert not (work / "references").exists()

    def test_does_not_overwrite_existing(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        skill = self._make_skill(tmp_path, with_refs=True)
        work = tmp_path / "work"
        work.mkdir()
        (work / "references").mkdir()
        (work / "references" / "existing.md").write_text("keep me", encoding="utf-8")
        _copy_skill_references(skill, work)
        assert (work / "references" / "existing.md").read_text(encoding="utf-8") == "keep me"
        assert not (work / "references" / "guide.md").exists()

    def test_none_skill_path_is_noop(self, tmp_path: Path):
        from skill_eval.execution.runner import _copy_skill_references
        work = tmp_path / "work"
        work.mkdir()
        _copy_skill_references(None, work)
        assert not (work / "references").exists()


class TestABTestRunnerReferencesAutoCopy:
    """P0-2 集成:references 依赖型 skill(未配 source_dir)开箱即用 ——
    with_skill 考场自动有 references/,without_skill 没有。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "myskill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: test skill\n---\n"
            "# 步骤\n\n参见 references/guide.md 了解领域知识。\n",
            encoding="utf-8",
        )
        (skill / "references").mkdir()
        (skill / "references" / "guide.md").write_text("domain knowledge", encoding="utf-8")
        return skill

    def test_with_skill_copies_references_without_does_not(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        skill = self._make_skill(tmp_path)

        # mock ClaudeRunner.run —— 不真跑 claude,返回最小 RunOutput
        def fake_run(self, prompt, skill_path=None, **kwargs):
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        config = RunConfig(
            runs_per_config=1,
            output_dir=tmp_path / "out",
            skip_compliance=True,
            skip_calibration=True,
            max_workers=1,
        )
        scoring = ScoringConfig(enable_programmatic=False, enable_llm=False)
        runner = ABTestRunner(config=config, scoring_config=scoring)
        eval_set = EvalSet(skill_name="myskill", evals=[
            EvalCase(id=1, prompt="do the task"),  # 故意不配 source_dir
        ])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        with_ref = tmp_path / "out" / "with_skill" / "eval-1" / "run-1" / "outputs" / "references" / "guide.md"
        without_ref = tmp_path / "out" / "without_skill" / "eval-1" / "run-1" / "outputs" / "references"
        assert with_ref.exists(), "with_skill 考场应自动拷 references/(body 引用了它)"
        assert not without_ref.exists(), "without_skill 不注入 body,不该拷 references/"


class TestABTestRunnerComplianceIsAudit:
    """遵循性引擎 = audit:开 compliance 时 with_skill 产 AuditReport 形 compliance.json,
    without_skill 不产;transcript.jsonl 落盘喂给 TranscriptAuditor。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "myskill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: test skill\n---\n"
            "# 步骤\n\n1. 必须读日志。\n\n## 禁止\n\n禁止编造错误码。\n",
            encoding="utf-8",
        )
        return skill

    def test_with_skill_compliance_is_audit_report(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            AuditReport, RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        skill = self._make_skill(tmp_path)

        # stdout 非空 → transcript.jsonl 落盘 → compliance 才会跑(实际 audit 被 mock,不真解析)
        def fake_run(self, prompt, skill_path=None, **kwargs):
            return RunOutput(exit_code=0, stdout='{"type":"result","result":"done"}',
                             transcript="done", result_text="done")

        canned = AuditReport(
            skill_name="myskill",
            transcripts=["transcript.jsonl"],
            summary={"total": 2, "pass": 1, "fail": 1, "na": 0, "unresolved": 0, "indeterminate": 0},
            findings=[],
        )

        config = RunConfig(
            runs_per_config=1,
            output_dir=tmp_path / "out",
            skip_compliance=False,
            skip_calibration=True,
            max_workers=1,
        )
        scoring = ScoringConfig(enable_programmatic=False, enable_llm=False)
        runner = ABTestRunner(config=config, scoring_config=scoring)
        eval_set = EvalSet(skill_name="myskill", evals=[EvalCase(id=1, prompt="do the task")])

        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run), \
             patch("skill_eval.audit.auditor.TranscriptAuditor") as MockAuditor:
            MockAuditor.return_value.audit.return_value = canned
            runner.run_ab_test(skill, eval_set)

        base = tmp_path / "out"
        with_comp = base / "with_skill" / "eval-1" / "run-1" / "compliance.json"
        without_comp = base / "without_skill" / "eval-1" / "run-1" / "compliance.json"
        assert with_comp.exists(), "with_skill 应产 compliance.json"
        assert not without_comp.exists(), "without_skill 不注入 skill,不该产 compliance.json"

        data = json.loads(with_comp.read_text(encoding="utf-8"))
        assert "summary" in data and "findings" in data, "compliance.json 应是 AuditReport 形(summary/findings)"
        assert "compliance_rate" not in data, "不再是旧 ComplianceReport 形(无 compliance_rate 顶层字段)"
        assert data["summary"] == {"total": 2, "pass": 1, "fail": 1, "na": 0, "unresolved": 0, "indeterminate": 0}

        # audit 被喂的是落盘的 transcript.jsonl
        called = MockAuditor.return_value.audit.call_args
        assert called.kwargs["skill_name"] == "myskill"
        assert [p.name for p in called.kwargs["transcripts"]] == ["transcript.jsonl"]


class TestBuildSides:
    """build_sides:sides spec 单一事实源(块 B v1/v2)。"""

    def test_default_with_without(self, tmp_path):
        from skill_eval.execution.runner import build_sides
        sk = tmp_path / "sk"
        sides = build_sides(sk, skill_b=None, label_a=None, label_b=None)
        assert sides == [("with_skill", sk, "with"), ("without_skill", None, "without")]

    def test_v1v2_basename_default_label(self, tmp_path):
        from skill_eval.execution.runner import build_sides
        a, b = tmp_path / "sk-a", tmp_path / "sk-b"
        a.mkdir(); b.mkdir()
        sides = build_sides(a, skill_b=b, label_a=None, label_b=None)
        assert sides == [("sk-a", a, "sk-a"), ("sk-b", b, "sk-b")]  # basename 作 label

    def test_v1v2_explicit_labels(self, tmp_path):
        from skill_eval.execution.runner import build_sides
        a, b = tmp_path / "sk-a", tmp_path / "sk-b"
        a.mkdir(); b.mkdir()
        sides = build_sides(a, skill_b=b, label_a="v1", label_b="v2")
        assert sides == [("v1", a, "v1"), ("v2", b, "v2")]


class TestABTestRunnerV1v2:
    """块 B:v1/v2 双 skill —— 两边各注入对应 skill,setup_source 钉主 skill 同卷。"""

    @staticmethod
    def _make_skill(path: Path, name: str, body: str = "body") -> Path:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\n{body}\n", encoding="utf-8"
        )
        return path

    def test_v1v2_injects_both_skills_and_uses_label_dirs(self, tmp_path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase

        skill_a = self._make_skill(tmp_path / "v1", "v1")
        skill_b = self._make_skill(tmp_path / "v2", "v2")

        injected: list = []  # 捕获每个 run 注入的 skill_path(side A=skill_a, side B=skill_b)

        def fake_run(self, prompt, skill_path=None, **kwargs):
            injected.append(skill_path)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out",
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="v1", evals=[EvalCase(id=1, prompt="do it")])
        sides = [("v1", skill_a, "v1"), ("v2", skill_b, "v2")]
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill_a, eval_set, sides=sides)

        # 两边都注入了各自的 skill(非 None)
        assert skill_a in injected and skill_b in injected, f"应各注入 skill_a/skill_b,实得 {injected}"
        # dir 名用 label
        assert (tmp_path / "out" / "v1").is_dir()
        assert (tmp_path / "out" / "v2").is_dir()
        assert not (tmp_path / "out" / "with_skill").exists()

    def test_same_paper_setup_source_pinned_to_primary(self, tmp_path):
        """setup_source 钉主 skill:两边 outputs 里 source fixture 都来自 skill_a(同卷)。"""
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase

        skill_a = self._make_skill(tmp_path / "v1", "v1")
        (skill_a / "data").mkdir()
        (skill_a / "data" / "log.txt").write_text("A-MARKER", encoding="utf-8")
        skill_b = self._make_skill(tmp_path / "v2", "v2")  # 无 data/
        # skill_b 放不同的 data 内容(若不钉主 skill,side B 会拿到 B-MARKER)
        (skill_b / "data").mkdir()
        (skill_b / "data" / "log.txt").write_text("B-MARKER", encoding="utf-8")

        def fake_run(self, prompt, skill_path=None, **kwargs):
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out",
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="v1", evals=[EvalCase(id=1, prompt="analyze", source_dir="data")])
        sides = [("v1", skill_a, "v1"), ("v2", skill_b, "v2")]
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill_a, eval_set, sides=sides)

        a_log = (tmp_path / "out" / "v1" / "eval-1" / "run-1" / "outputs" / "data" / "log.txt")
        b_log = (tmp_path / "out" / "v2" / "eval-1" / "run-1" / "outputs" / "data" / "log.txt")
        assert a_log.exists() and b_log.exists()
        # 两边都应是主 skill(skill_a)的 A-MARKER —— 同卷保证
        assert a_log.read_text(encoding="utf-8") == "A-MARKER"
        assert b_log.read_text(encoding="utf-8") == "A-MARKER", "side B 应钉主 skill 拿同一份源材料,非 skill_b 的"

    def test_default_mode_injects_none_for_without(self, tmp_path):
        """默认 sides(with/without):without 侧注入 None(skill 关),向后兼容。"""
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase

        skill = self._make_skill(tmp_path / "sk", "sk")
        injected: list = []

        def fake_run(self, prompt, skill_path=None, **kwargs):
            injected.append(skill_path)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out",
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="do")])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)  # 默认 sides=None

        assert skill in injected and None in injected, f"默认应注入 skill + None,实得 {injected}"
        assert (tmp_path / "out" / "with_skill").is_dir()
        assert (tmp_path / "out" / "without_skill").is_dir()


class TestABTestRunnerPerEvalTimeout:
    """P1-3a: per-eval timeout 覆盖全局(混跑时重任务给大、轻任务走默认)。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        return skill

    def test_per_eval_timeout_overrides_global(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        captured: list[int | None] = []

        def fake_run(self, prompt, skill_path=None, timeout=None, **kwargs):
            captured.append(timeout)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", timeout_seconds=300,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[
            EvalCase(id=1, prompt="simple"),               # 无 timeout → 全局 300
            EvalCase(id=2, prompt="design", timeout=600),  # per-eval → 600
        ])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        # 2 evals × 2 configs = 4 runs;eval1 全 300,eval2 全 600
        assert 300 in captured, "无 per-eval timeout 的 eval 应走全局 300"
        assert 600 in captured, "有 per-eval timeout 的 eval 应覆盖为 600"
        assert all(t in (300, 600) for t in captured), f"意外 timeout: {captured}"

    def test_no_per_eval_timeout_falls_back_to_global(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import (
            RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase,
        )

        captured: list[int | None] = []

        def fake_run(self, prompt, skill_path=None, timeout=None, **kwargs):
            captured.append(timeout)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", timeout_seconds=250,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x")])  # 都不配 timeout
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        assert all(t == 250 for t in captured), f"无 per-eval 应全部走全局 250,实际: {captured}"


class TestABTestRunnerRetry:
    """P2-2: max_retries_per_eval 控制 retry 次数(error 触发)。
    CLI --max-retries 透传 RunConfig.max_retries_per_eval,字段直接驱动此行为。"""

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        return skill

    def test_zero_retries_no_retry_on_error(self, tmp_path: Path):
        """max_retries=0 → error 不 retry,每 job 只调 1 次 claude。"""
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, EvalSet, EvalCase
        from skill_eval.errors import ClaudeRunnerError

        call_count = [0]

        def fake_run(self, prompt, skill_path=None, **kwargs):
            call_count[0] += 1
            raise ClaudeRunnerError("boom")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", max_retries_per_eval=0,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x")])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        # 1 eval × 2 config × 1 run = 2 jobs;error 不 retry → 共 2 次调用
        assert call_count[0] == 2, f"max_retries=0 不该 retry,期望 2 次调用(2 job),实际 {call_count[0]}"

    def test_two_retries_three_calls_per_job_on_error(self, tmp_path: Path):
        """max_retries=2 → error 重试到 3 次/job(original + retry×2)。"""
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, EvalSet, EvalCase
        from skill_eval.errors import ClaudeRunnerError

        call_count = [0]

        def fake_run(self, prompt, skill_path=None, **kwargs):
            call_count[0] += 1
            raise ClaudeRunnerError("boom")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out", max_retries_per_eval=2,
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x")])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        # 2 job × (1 + 2 retry) = 6 次调用
        assert call_count[0] == 6, f"max_retries=2 该 3 次/job,期望 6 次调用(2 job),实际 {call_count[0]}"


class TestRunDumpsTranscript:
    """run 把 raw stream-json dump 成 transcript.jsonl,打通 live 真跑→audit 闭环。

    背景:`_parse_stream_json` 只把 assistant text 块拼进 transcript、tool_use 只记截断
    input_summary,丢了 tool_use 完整 input + tool_result 配对;而 audit 的 build_digest
    需要完整配对。解法:额外 dump raw stream-json 成 transcript.jsonl。stream-json 的
    assistant/user event 本身带 message 字段 → build_digest 可直接解析(result event
    无 message,自动跳过)。使 `run`(路径1/3)产物能喂 `audit`(路径2)/`init --from-transcript`。
    """

    @staticmethod
    def _make_skill(tmp_path: Path) -> Path:
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: t\ndescription: test skill\n---\nbody\n", encoding="utf-8"
        )
        return skill

    def test_dumps_transcript_parsable_by_build_digest(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        from skill_eval.models import RunConfig, ScoringConfig, RunOutput, EvalSet, EvalCase
        from skill_eval.evals.transcript import build_digest

        # 仿 claude -p --output-format stream-json 的 stdout:assistant(tool_use Read)
        # + user(tool_result) + result(无 message,build_digest 应跳过)
        stream_json = "\n".join([
            '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"read it"},{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/x/f.txt"}}]}}',
            '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"hello"}]}}',
            '{"type":"result","result":"done","usage":{"input_tokens":10,"output_tokens":5},"num_turns":1,"stop_reason":"end_turn"}',
        ])

        def fake_run(self, prompt, skill_path=None, **kwargs):
            return RunOutput(exit_code=0, stdout=stream_json, transcript="read it", result_text="done")

        skill = self._make_skill(tmp_path)
        config = RunConfig(
            runs_per_config=1, output_dir=tmp_path / "out",
            skip_compliance=True, skip_calibration=True, max_workers=1,
        )
        runner = ABTestRunner(config=config, scoring_config=ScoringConfig(enable_programmatic=False, enable_llm=False))
        eval_set = EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x")])
        with patch("skill_eval.execution.runner.ClaudeRunner.run", fake_run):
            runner.run_ab_test(skill, eval_set)

        tp = tmp_path / "out" / "with_skill" / "eval-1" / "run-1" / "transcript.jsonl"
        assert tp.exists(), "run 应 dump transcript.jsonl(raw stream-json stdout)"
        # build_digest 直接解析 stream-json(assistant/user event 带 message;result 跳过)
        d = build_digest(tp)
        assert any(t.name == "Read" for t in d.tool_trace), \
            f"build_digest 应从 stream-json 解析出 Read,实际 tool_trace={d.tool_trace}"
        assert "read it" in d.transcript_text


class TestCacheKey:
    """内容感知缓存键(_compute_cache_key):改 SKILL 正文 / eval / model-config 自动失效;
    BASE 侧(inject_skill=None)不认 skill_body,改 SKILL.md 只重跑 with 侧。"""

    @staticmethod
    def _skill(parent: Path, name: str, body: str) -> Path:
        d = parent / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: x\n---\n{body}", encoding="utf-8")
        return d

    @staticmethod
    def _base():
        from skill_eval.models import EvalCase, RunConfig, ScoringConfig
        ec = EvalCase(id=1, prompt="do it", expectations=["步骤正确"])
        cfg = RunConfig(model="m1", max_turns=10, timeout_seconds=120)
        sc = ScoringConfig(enable_programmatic=True, enable_llm=True, llm_model="m1")
        return ec, cfg, sc

    def test_grading_result_cache_key_default_empty(self):
        from skill_eval.models import GradingResult, GradingSummary
        g = GradingResult(summary=GradingSummary(pass_rate=0.0, passed=0, failed=0, total=0))
        assert g.cache_key == "", "GradingResult 应有 cache_key 字段,默认空串(老 grading.json 兼容)"

    def test_stable(self, tmp_path: Path):
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        skill = self._skill(tmp_path, "s", "# 步骤\n1. 做 A")
        k1 = _compute_cache_key(ec, skill, cfg, sc)
        k2 = _compute_cache_key(ec, skill, cfg, sc)
        assert k1 == k2 and len(k1) == 16, "同输入应得同 key(16 hex)"

    def test_skill_body_change_invalidates_with_side(self, tmp_path: Path):
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        sA = self._skill(tmp_path / "a", "s", "# 步骤\n1. 做 A")
        sB = self._skill(tmp_path / "b", "s", "# 步骤\n1. 做 B(改了指令)")
        assert _compute_cache_key(ec, sA, cfg, sc) != _compute_cache_key(ec, sB, cfg, sc), \
            "with 侧改了 SKILL 正文,cache_key 必须变"

    def test_base_ignores_skill_body(self, tmp_path: Path):
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        sA = self._skill(tmp_path / "a", "s", "# 步骤\n1. 做 A")
        sB = self._skill(tmp_path / "b", "s", "# 完全不同的步骤 ZZZ")
        # BASE 侧 inject_skill=None → skill_body="" → 与 skill 内容无关
        assert _compute_cache_key(ec, None, cfg, sc) == _compute_cache_key(ec, None, cfg, sc), \
            "BASE 侧 key 不该随 skill 变"
        # 对照:with 侧注入不同 body → key 必变
        assert _compute_cache_key(ec, sA, cfg, sc) != _compute_cache_key(ec, sB, cfg, sc)

    def test_eval_change_invalidates(self, tmp_path: Path):
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        skill = self._skill(tmp_path, "s", "# 步骤\n1. A")
        ec_prompt = ec.model_copy(update={"prompt": "do it differently"})
        ec_expec = ec.model_copy(update={"expectations": ["步骤正确", "新增一条"]})
        assert _compute_cache_key(ec, skill, cfg, sc) != _compute_cache_key(ec_prompt, skill, cfg, sc)
        assert _compute_cache_key(ec, skill, cfg, sc) != _compute_cache_key(ec_expec, skill, cfg, sc)

    def test_model_config_change_invalidates(self, tmp_path: Path):
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        skill = self._skill(tmp_path, "s", "# 步骤\n1. A")
        cfg2 = cfg.model_copy(update={"model": "m2"})
        sc2 = sc.model_copy(update={"enable_llm": False})
        assert _compute_cache_key(ec, skill, cfg, sc) != _compute_cache_key(ec, skill, cfg2, sc)
        assert _compute_cache_key(ec, skill, cfg, sc) != _compute_cache_key(ec, skill, cfg, sc2)

    def test_code_version_hash_is_stable_nonempty(self):
        """_code_version_hash 聚合 src/skill_eval 全部 .py,进程内稳定、16 hex、非空。"""
        from skill_eval.execution.runner import _code_version_hash
        h1 = _code_version_hash()
        h2 = _code_version_hash()
        assert h1 and len(h1) == 16 and h1 == h2, \
            "code 版本 hash 应非空、16 hex、进程内稳定(只算一次)"

    def test_code_version_change_invalidates(self, tmp_path: Path, monkeypatch):
        """工具自身源码升级(修 runner/grader bug)→ cache_key 必须变。
        否则默认目录重跑命中旧 grading.json、全 skip(2026-07-18 用户撞的 skip:18)。
        进程内代码不变 → _code_version_hash 模块级缓存,monkeypatch 模拟「代码变了」。"""
        from skill_eval.execution import runner as runner_mod
        from skill_eval.execution.runner import _compute_cache_key
        ec, cfg, sc = self._base()
        skill = self._skill(tmp_path, "s", "# 步骤\n1. A")
        monkeypatch.setattr(runner_mod, "_CODE_HASH_CACHE", "aaaa1111", raising=False)
        k_old = _compute_cache_key(ec, skill, cfg, sc)
        monkeypatch.setattr(runner_mod, "_CODE_HASH_CACHE", "bbbb2222", raising=False)
        k_new = _compute_cache_key(ec, skill, cfg, sc)
        assert k_old != k_new, "工具源码变了,cache_key 必须随之失效重跑,否则改了代码默认目录重跑仍复用旧结果"


class TestResumeCacheKey:
    """续跑缓存键接入 skip 路径:cache_key 匹配→跳过、不匹配→重跑、老格式(无 key)→重跑。"""

    @staticmethod
    def _skill(tmp_path: Path) -> Path:
        d = tmp_path / "myskill"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: x\n---\n# 步骤\n1. 做 A\n", encoding="utf-8")
        return d

    @staticmethod
    def _slot_grading_path(output_dir: Path) -> Path:
        return output_dir / "with_skill" / "eval-1" / "run-1" / "grading.json"

    def _setup(self, tmp_path: Path):
        from skill_eval.models import RunConfig, ScoringConfig, EvalSet, EvalCase
        skill = self._skill(tmp_path)
        output_dir = tmp_path / "out"
        ec = EvalCase(id=1, prompt="do it", expectations=["步骤正确"])
        config = RunConfig(runs_per_config=1, output_dir=output_dir,
                           skip_compliance=True, skip_calibration=True, max_workers=1,
                           model="m1", max_turns=10, timeout_seconds=120)
        scoring = ScoringConfig(enable_programmatic=False, enable_llm=False, llm_model="m1")
        eval_set = EvalSet(skill_name="myskill", evals=[ec])
        return skill, output_dir, ec, config, scoring, eval_set

    @staticmethod
    def _fake_run_factory(calls: list):
        from skill_eval.models import RunOutput
        def fake_run(self, prompt, skill_path=None, **kwargs):
            calls.append((prompt, skill_path))   # 记 skill_path 区分 with(=skill)/without(=None)
            return RunOutput(exit_code=0, stdout="ok", transcript="done", result_text="done")
        return fake_run

    def _with_skill_ran(self, calls, skill) -> bool:
        return any(sp == skill for _, sp in calls)

    def test_match_skips(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner, _compute_cache_key
        from skill_eval.models import GradingResult, GradingSummary, ExpectationResult
        skill, output_dir, ec, config, scoring, eval_set = self._setup(tmp_path)
        key = _compute_cache_key(ec, skill, config, scoring)
        gp = self._slot_grading_path(output_dir)
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(GradingResult(
            expectations=[ExpectationResult(text="ok", passed=True, evidence="")],
            summary=GradingSummary(pass_rate=1.0, passed=1, failed=0, total=1),
            cache_key=key,
        ).model_dump_json(indent=2), encoding="utf-8")

        calls: list = []
        runner = ABTestRunner(config=config, scoring_config=scoring)
        with patch("skill_eval.execution.runner.ClaudeRunner.run", self._fake_run_factory(calls)):
            runner.run_ab_test(skill, eval_set)

        assert not self._with_skill_ran(calls, skill), "cache_key 匹配 + total>0 → with 槽应跳过"
        g = GradingResult.model_validate(json.loads(gp.read_text(encoding="utf-8")))
        assert g.summary.total == 1 and g.cache_key == key, "跳过时 grading.json 不被覆写"

    def test_mismatch_reruns(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner, _compute_cache_key
        from skill_eval.models import GradingResult, GradingSummary, ExpectationResult
        skill, output_dir, ec, config, scoring, eval_set = self._setup(tmp_path)
        gp = self._slot_grading_path(output_dir)
        gp.parent.mkdir(parents=True, exist_ok=True)
        gp.write_text(GradingResult(
            expectations=[ExpectationResult(text="ok", passed=True, evidence="")],
            summary=GradingSummary(pass_rate=1.0, passed=1, failed=0, total=1),
            cache_key="WRONG_STALE_KEY",
        ).model_dump_json(indent=2), encoding="utf-8")
        expected_key = _compute_cache_key(ec, skill, config, scoring)

        calls: list = []
        runner = ABTestRunner(config=config, scoring_config=scoring)
        with patch("skill_eval.execution.runner.ClaudeRunner.run", self._fake_run_factory(calls)):
            runner.run_ab_test(skill, eval_set)

        assert self._with_skill_ran(calls, skill), "cache_key 不匹配 → with 槽应重跑"
        g = GradingResult.model_validate(json.loads(gp.read_text(encoding="utf-8")))
        assert g.cache_key == expected_key, "重跑后 grading.json 应写上正确 cache_key"

    def test_legacy_no_key_reruns(self, tmp_path: Path):
        from skill_eval.execution.runner import ABTestRunner
        skill, output_dir, ec, config, scoring, eval_set = self._setup(tmp_path)
        gp = self._slot_grading_path(output_dir)
        gp.parent.mkdir(parents=True, exist_ok=True)
        # 老格式:无 cache_key 字段(模拟升级前的 grading.json)
        gp.write_text(json.dumps({
            "expectations": [{"text": "ok", "passed": True, "evidence": ""}],
            "summary": {"pass_rate": 1.0, "passed": 1, "failed": 0, "total": 1},
        }, ensure_ascii=False), encoding="utf-8")

        calls: list = []
        runner = ABTestRunner(config=config, scoring_config=scoring)
        with patch("skill_eval.execution.runner.ClaudeRunner.run", self._fake_run_factory(calls)):
            runner.run_ab_test(skill, eval_set)

        assert self._with_skill_ran(calls, skill), "老 grading.json 无 cache_key → 向后兼容失效重跑"


class TestCacheNotice:
    """全跳过/部分跳过的缓存复用提示行(_cache_notice_lines)。"""

    @staticmethod
    def _state(skipped, total):
        from types import SimpleNamespace
        return SimpleNamespace(skipped=skipped, total_runs=total)

    def test_no_skip_no_notice(self, tmp_path: Path):
        from skill_eval.cli.pipeline import _cache_notice_lines
        assert _cache_notice_lines(self._state(0, 18), tmp_path) == []

    def test_all_skipped_two_yellow_lines(self, tmp_path: Path):
        from skill_eval.cli.pipeline import _cache_notice_lines
        lines = _cache_notice_lines(self._state(18, 18), tmp_path)
        assert len(lines) == 2
        assert "全部 18 个槽" in lines[0] and "[yellow]" in lines[0]
        assert str(tmp_path) in lines[1], "提示应指明清哪个目录"

    def test_partial_skipped_one_dim_line(self, tmp_path: Path):
        from skill_eval.cli.pipeline import _cache_notice_lines
        lines = _cache_notice_lines(self._state(6, 18), tmp_path)
        assert len(lines) == 1
        assert "6/18" in lines[0] and "[dim]" in lines[0]


class TestProgressCallbackWiring:
    """on_progress → progress_state.update 参数必须逐个对齐。
    曾漏传 total_evals 致整体左移:update.grading 收到 run_duration(float)→ completion 渲染崩。"""

    def test_forwards_total_evals_and_grading(self):
        from skill_eval.cli.pipeline import _make_progress_callback
        from skill_eval.cli.progress import _ProgressState
        from skill_eval.models import GradingResult, GradingSummary

        ps = _ProgressState(2, 1)
        captured = {}

        def spy(eval_name, config_name, eval_idx, total_evals, completed, total,
                grading, run_duration=0.0, run_output=None, phase="", substep=""):
            captured.update(total_evals=total_evals, completed=completed, total=total,
                            grading=grading, run_duration=run_duration, phase=phase)
        ps.update = spy  # 拦截,看 update 实际收到什么

        cb = _make_progress_callback(ps)
        g = GradingResult(summary=GradingSummary(pass_rate=1.0, passed=1, failed=0, total=1))
        cb("eval-1", "with_skill", 1, 2, 1, 2, g, 5.0, None, phase="executing")

        assert captured["total_evals"] == 2, "total_evals 必须透传(曾漏传→左移)"
        assert captured["grading"] is g, "grading 必须原样转发,不能错位成 run_duration"
        assert captured["completed"] == 1 and captured["total"] == 2
        assert captured["run_duration"] == 5.0 and captured["phase"] == "executing"


class TestRerunClassification:
    """Task 3:重跑归档分类(resume/accumulate/fresh)+ archive_previous 单槽 MOVE。

    决策表(见与用户敲定):
    - benchmark.json 不在 → resume(cache 续跑,不动)
    - benchmark.json 在 + inputs 没改 + runs 调大 → accumulate(复用旧 slot + 补 run 号,不归档)
    - benchmark.json 在 + inputs 改了 或 runs 没调大 → fresh(MOVE 旧树到 previous/ + fresh 重跑)
    """

    @staticmethod
    def _slot(parent: Path, config_name: str, eval_id: int, run_num: int, cache_key: str):
        d = parent / config_name / f"eval-{eval_id}" / f"run-{run_num}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "outputs").mkdir(exist_ok=True)
        (d / "grading.json").write_text(json.dumps({
            "expectations": [],
            "summary": {"passed": 0, "failed": 0, "total": 0, "pass_rate": 0.0},
            "cache_key": cache_key,
        }), encoding="utf-8")

    @staticmethod
    def _inputs(tmp_path: Path):
        """BASE 侧(inject_skill=None → cache_key 不含 skill_body,简化测试)。"""
        from skill_eval.models import EvalCase, RunConfig, ScoringConfig
        from skill_eval.execution.runner import _compute_cache_key
        ec = EvalCase(id=1, prompt="do it")
        sides = [("without_skill", None, "without")]
        config = RunConfig(model="m1", max_turns=10, timeout_seconds=120)
        scoring = ScoringConfig()
        key = _compute_cache_key(ec, None, config, scoring)
        return ec, sides, config, scoring, key

    def test_no_benchmark_means_resume(self, tmp_path: Path):
        """benchmark.json 不在(中断/首次)→ resume,即便有零散 slot 也走续跑。"""
        from skill_eval.execution.runner import classify_rerun
        ec, sides, config, scoring, key = self._inputs(tmp_path)
        self._slot(tmp_path, "without_skill", 1, 1, key)  # 有 slot 但无 benchmark = 中断态
        assert classify_rerun(tmp_path, [ec], sides, 1, config, scoring) == "resume"

    def test_same_runs_inputs_unchanged_means_fresh(self, tmp_path: Path):
        """啥也没改、同 runs 重跑 → fresh(用户要看方差,MOVE + fresh)。"""
        from skill_eval.execution.runner import classify_rerun
        ec, sides, config, scoring, key = self._inputs(tmp_path)
        self._slot(tmp_path, "without_skill", 1, 1, key)
        (tmp_path / "benchmark.json").write_text("{}", encoding="utf-8")
        assert classify_rerun(tmp_path, [ec], sides, 1, config, scoring) == "fresh"

    def test_more_runs_inputs_unchanged_means_accumulate(self, tmp_path: Path):
        """inputs 没改 + runs 调大 → accumulate(复用旧 slot + 补 run 号,不归档)。"""
        from skill_eval.execution.runner import classify_rerun
        ec, sides, config, scoring, key = self._inputs(tmp_path)
        self._slot(tmp_path, "without_skill", 1, 1, key)  # 旧 run-1
        (tmp_path / "benchmark.json").write_text("{}", encoding="utf-8")
        assert classify_rerun(tmp_path, [ec], sides, 3, config, scoring) == "accumulate"

    def test_inputs_changed_means_fresh_even_if_more_runs(self, tmp_path: Path):
        """inputs 改了(cache_key 不匹配)→ 即使 runs 调大也 fresh(旧样本是旧输入产物,不能 accumulate)。"""
        from skill_eval.execution.runner import classify_rerun
        ec, sides, config, scoring, _key = self._inputs(tmp_path)
        self._slot(tmp_path, "without_skill", 1, 1, "STALE_KEY_FROM_OLD_INPUT")
        (tmp_path / "benchmark.json").write_text("{}", encoding="utf-8")
        assert classify_rerun(tmp_path, [ec], sides, 3, config, scoring) == "fresh"

    def test_side_no_longer_present_means_fresh(self, tmp_path: Path):
        """旧 config 名不在当前 sides(v1/v2 换了等)→ 结构变 → fresh。"""
        from skill_eval.execution.runner import classify_rerun
        ec, sides, config, scoring, _key = self._inputs(tmp_path)
        # 旧 run 落在 with_skill(当前 sides 只有 without_skill)
        self._slot(tmp_path, "with_skill", 1, 1, "whatever")
        (tmp_path / "benchmark.json").write_text("{}", encoding="utf-8")
        assert classify_rerun(tmp_path, [ec], sides, 3, config, scoring) == "fresh"


class TestArchivePrevious:
    """archive_previous:把当前 run 树整体 MOVE 到 previous/(单槽覆盖)。"""

    def test_moves_whole_tree_to_previous(self, tmp_path: Path):
        from skill_eval.execution.runner import archive_previous
        # 造一棵完整旧树:config 目录 + 顶层 artifacts
        (tmp_path / "without_skill" / "eval-1" / "run-1" / "outputs").mkdir(parents=True)
        (tmp_path / "without_skill" / "eval-1" / "run-1" / "grading.json").write_text("{}", encoding="utf-8")
        (tmp_path / "benchmark.json").write_text('{"old": true}', encoding="utf-8")
        (tmp_path / "scorecard.json").write_text("{}", encoding="utf-8")

        prev = archive_previous(tmp_path)

        assert prev == tmp_path / "previous"
        # 顶层散文件 + config 目录都进 previous/
        assert (prev / "benchmark.json").read_text() == '{"old": true}'
        assert (prev / "scorecard.json").exists()
        assert (prev / "without_skill" / "eval-1" / "run-1" / "grading.json").exists()
        # output 顶层已被清空(只剩 previous/)
        assert [p.name for p in tmp_path.iterdir()] == ["previous"]

    def test_single_slot_overwrites_old_previous(self, tmp_path: Path):
        """连续两次 fresh 重跑:第二次归档覆盖第一次的 previous/(单槽,不堆积)。"""
        from skill_eval.execution.runner import archive_previous
        # 第一次归档留下的 previous/
        (tmp_path / "previous" / "without_skill" / "eval-1" / "run-1").mkdir(parents=True)
        (tmp_path / "previous" / "benchmark.json").write_text('{"first": true}', encoding="utf-8")
        # 第二次的新树
        (tmp_path / "without_skill" / "eval-2" / "run-1").mkdir(parents=True)
        (tmp_path / "benchmark.json").write_text('{"second": true}', encoding="utf-8")

        archive_previous(tmp_path)

        prev = tmp_path / "previous"
        assert (prev / "benchmark.json").read_text() == '{"second": true}'  # 覆盖,非第一次
        assert (prev / "without_skill" / "eval-2").exists()
        assert not (prev / "without_skill" / "eval-1").exists(), "旧 previous/ 必须整体清掉,不能混进新归档"

    def test_empty_output_returns_none(self, tmp_path: Path):
        """output 空无可移(或只剩 previous/)→ 返回 None,不动。"""
        from skill_eval.execution.runner import archive_previous
        (tmp_path / "previous").mkdir()  # 只剩 previous/(无他物)
        assert archive_previous(tmp_path) is None
        assert (tmp_path / "previous").exists()

    def test_preserves_non_run_artifacts(self, tmp_path: Path):
        """fresh 归档只 move run 自己的产物,不卷走同目录其他命令(audit/gate/report)的产物——
        它们与本次 run 无绑定,误伤会丢用户数据。"""
        from skill_eval.execution.runner import archive_previous
        # run 自己的产物
        (tmp_path / "without_skill" / "eval-1" / "run-1").mkdir(parents=True)
        (tmp_path / "without_skill" / "eval-1" / "run-1" / "grading.json").write_text("{}", encoding="utf-8")
        (tmp_path / "benchmark.json").write_text("{}", encoding="utf-8")
        (tmp_path / "scorecard.json").write_text("{}", encoding="utf-8")
        (tmp_path / "vet-report.json").write_text("{}", encoding="utf-8")
        # 其他命令落在同目录的产物(不该被卷)
        (tmp_path / "audit-report.json").write_text("{}", encoding="utf-8")
        (tmp_path / "audit-report.html").write_text("<html></html>", encoding="utf-8")
        (tmp_path / "slices").mkdir()
        (tmp_path / "slices" / "slice-0.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "gate.json").write_text("{}", encoding="utf-8")
        (tmp_path / "report.html").write_text("<html></html>", encoding="utf-8")

        archive_previous(tmp_path)

        prev = tmp_path / "previous"
        # run 产物进了 previous/
        assert (prev / "benchmark.json").exists()
        assert (prev / "scorecard.json").exists()
        assert (prev / "vet-report.json").exists()
        assert (prev / "without_skill" / "eval-1" / "run-1" / "grading.json").exists()
        # 其他命令产物原地不动(没被卷进 previous/)
        assert (tmp_path / "audit-report.json").exists()
        assert (tmp_path / "audit-report.html").exists()
        assert (tmp_path / "slices" / "slice-0.jsonl").exists()
        assert (tmp_path / "gate.json").exists()
        assert (tmp_path / "report.html").exists()
        assert not (prev / "audit-report.json").exists()
        assert not (prev / "slices").exists()
        assert not (prev / "gate.json").exists()
        assert not (prev / "report.html").exists()

    def test_only_non_run_artifacts_returns_none(self, tmp_path: Path):
        """output 里只有别命令产物(无 run 产物)→ 无可归档,返回 None,
        且不建 previous/、不动那些产物。"""
        from skill_eval.execution.runner import archive_previous
        (tmp_path / "audit-report.json").write_text("{}", encoding="utf-8")
        (tmp_path / "gate.json").write_text("{}", encoding="utf-8")
        assert archive_previous(tmp_path) is None
        assert (tmp_path / "audit-report.json").exists()
        assert (tmp_path / "gate.json").exists()
        assert not (tmp_path / "previous").exists()
