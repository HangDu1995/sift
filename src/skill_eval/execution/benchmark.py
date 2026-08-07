"""Benchmark aggregator — merges run results into benchmark.json."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

from skill_eval.models import (
    AnomalyReport,
    BenchmarkData,
    BenchmarkMetadata,
    CalibrationReport,
    CalibrationSummary,
    ComparisonSummary,
    RunAnomaly,
    RunResultEntry,
    RunResultMetrics,
    StatsValue,
)
from skill_eval.parser import parse_skill_md
from skill_eval.execution.comparison_suite import run_comparisons
from skill_eval.execution.analysis_suite import run_analysis
from skill_eval.utils import TokenUsageAccumulator, DurationAccumulator
from skill_eval.execution.run_paths import index_runs, is_nested
from skill_eval.stats import normalized_gain


def calculate_stats(values: list[float]) -> StatsValue:
    if not values:
        return StatsValue(mean=0.0, stddev=0.0, min=0.0, max=0.0)
    n = len(values)
    mean = sum(values) / n
    stddev = math.sqrt(sum((x - mean) ** 2 for x in values) / (n - 1)) if n > 1 else 0.0
    return StatsValue(
        mean=round(mean, 4),
        stddev=round(stddev, 4),
        min=round(min(values), 4),
        max=round(max(values), 4),
    )


def _real_model_from_transcript(run_dir: Path) -> str:
    """从 transcript.jsonl 的 result event.modelUsage key 抓真实模型名。

    transcript.jsonl 是 cc stream-json 原文落盘,result event 顶层 modelUsage =
    {真实模型名: {token 明细}};取第一个 key(常规单模型)。无 transcript / 无
    result event / 解析失败 → ""。comparator/analyzer 不经 cc stream-json、
    不落 transcript.jsonl,故三模型字段都借 executor 抓的这个真名(三者同源:同一 CLI/env 解析)。
    """
    tp = run_dir / "transcript.jsonl"
    if not tp.exists():
        return ""
    try:
        for line in tp.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                mu = ev.get("modelUsage") or {}
                if isinstance(mu, dict) and mu:
                    return next(iter(mu))
    except Exception:
        pass
    return ""


def _collect_per_side_calibration(iteration_dir: Path, sides: list | None) -> dict:
    """按 side 读 per-run calibration.json,聚合返回 `{label: [(eval_id, run_n, CalibrationReport), ...] | None}`。

    sides 真实结构(见 runner.build_sides):`[(config_name, inject_skill_path_or_None, label), ...]`。
    config 目录 = `iteration_dir / config_name`(tuple 里**不存**目录,只存 config_name)。
    label 作 dict key(gate 消费侧认 label:"with"/"without" 或 "v1"/"v2")。

    index_runs 取最终 retry 去重(防 retry-glob bug 类:base+retry 只算最终 retry)。
    缺 calibration.json / 解析失败 → 该 run 跳过(不抛);整侧无报告 → 该 side None。
    sides=None → 落默认 [("with_skill", None, "with"), ("without_skill", None, "without")]。
    """
    if not sides:
        sides = [("with_skill", None, "with"), ("without_skill", None, "without")]
    per_side: dict[str, list[tuple[int, int, CalibrationReport]] | None] = {}
    for config_name, _inject_skill, label in sides:
        cfg_dir = iteration_dir / config_name
        if not cfg_dir.is_dir():
            per_side[label] = None
            continue
        run_map = index_runs(cfg_dir, is_nested(cfg_dir))
        reports: list[tuple[int, int, CalibrationReport]] = []
        for (eval_name, run_n), run_dir in run_map.items():
            cal_path = run_dir / "calibration.json"
            if not cal_path.exists():
                continue
            try:
                rep = CalibrationReport.model_validate_json(cal_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            # eval_name 形如 "eval-1"(nested)或 None(flat 单桶);→ 数字 eval_id
            eval_id = int(str(eval_name).split("-")[-1]) if eval_name else 0
            reports.append((eval_id, run_n, rep))
        per_side[label] = reports or None
    return per_side


class BenchmarkAggregator:
    def __init__(self, model: str | None = None, timeout: int = 120, enable_llm: bool = True):
        self.model = model
        self.timeout = timeout
        # 盲比较只服务于主观题;programmatic-only(enable_llm=False)时 run_comparisons 跳过
        self.enable_llm = enable_llm

    def aggregate(self, iteration_dir: Path, skill_path: Path = Path(), sides: list | None = None, phase1_wall: float = 0.0) -> BenchmarkData:
        import sys
        # phase2 墙钟:aggregate 自身耗时(读 run 产物 + comparator + analyzer + 字段构建)。
        # total = phase1_wall(调用方传,从命令起点含 setup)+ phase2_wall,严格加总(见末尾)。
        _p2_start = time.perf_counter()
        sys.stderr.write("aggregate: reading run results...\n")
        sys.stderr.flush()

        name = ""
        if skill_path and (skill_path / "SKILL.md").exists():
            name, _, _, _, _ = parse_skill_md(skill_path)

        runs: list[RunResultEntry] = []
        configs_data: dict[str, list[dict]] = {}
        anomalies: list[RunAnomaly] = []
        duration_map: dict[str, list[dict]] = {}
        real_model = ""  # 从 transcript 抓的真实模型名(executor 落盘,三模型字段复用)
        # Phase 2: LLM token 累加(scoring = grader+comparator+analyzer 分计;audit = compliance rollup)
        comparator_acc = TokenUsageAccumulator()  # 盲比较(run_comparisons 回灌)
        analyzer_acc = TokenUsageAccumulator()    # 深度分析(run_analysis 回灌)
        grader_acc = TokenUsageAccumulator()      # per-run grader(timing.json 汇总)
        audit_acc = TokenUsageAccumulator()       # per-run compliance.json rollup
        executor_total = 0                        # per-run executor token(timing.json total_tokens)
        # Phase 2.5: 耗时累加(分列=各类调用时长加总;total=墙钟,独立测,见末尾 rollup)
        comparator_dur = DurationAccumulator()    # 盲比较
        analyzer_dur = DurationAccumulator()      # 深度分析
        grader_dur = DurationAccumulator()        # per-run grader(timing.json grader_duration_seconds)
        audit_dur = DurationAccumulator()         # per-run compliance.json duration_seconds
        executor_dur = 0.0                        # per-run executor 时长(timing total_duration_seconds)

        for config_dir in sorted(iteration_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            config_name = config_dir.name

            # 一个 run = (eval, run_number) 的最终尝试;glob 会匹配 retry 目录,
            # 用 index_runs 只取最终 retry(否则被取代的失败 base 当独立低分 run 计入)。
            run_dirs = list(index_runs(config_dir, is_nested(config_dir)).values())
            if not run_dirs:
                continue

            if config_name not in configs_data:
                configs_data[config_name] = []

            for run_dir in run_dirs:
                if not real_model:
                    real_model = _real_model_from_transcript(run_dir)
                run_number = int(run_dir.name.split("-")[1])
                retry_count = 0
                if "-retry-" in run_dir.name:
                    try:
                        retry_count = int(run_dir.name.rsplit("-retry-", 1)[1])
                    except (ValueError, IndexError):
                        pass
                eval_id = 0
                parent_name = run_dir.parent.name
                if parent_name.startswith("eval-"):
                    try:
                        eval_id = int(parent_name.split("-")[1])
                    except (ValueError, IndexError):
                        pass

                grading_path = run_dir / "grading.json"
                if not grading_path.exists():
                    continue

                grading = json.loads(grading_path.read_text(encoding="utf-8"))
                summary = grading.get("summary", {})

                timing = grading.get("timing") or {}
                time_seconds = timing.get("total_duration_seconds", 0.0)
                tokens = timing.get("total_tokens", 0)

                timing_path = run_dir / "timing.json"
                if time_seconds == 0.0 and timing_path.exists():
                    td = json.loads(timing_path.read_text(encoding="utf-8"))
                    time_seconds = td.get("total_duration_seconds", 0.0)
                    tokens = td.get("total_tokens", tokens)

                # Phase 2: per-run token 累加 / Phase 2.5: per-run 时长累加
                executor_total += tokens
                executor_dur += time_seconds  # timing.total_duration_seconds(:178 已读)
                if timing_path.exists():
                    try:
                        _tj = json.loads(timing_path.read_text(encoding="utf-8"))
                        gtu = _tj.get("grader_token_usage") or {}
                        if gtu:
                            grader_acc.add(gtu, count=gtu.get("calls", 1))
                        grader_dur.add(_tj.get("grader_duration_seconds") or 0.0)
                    except Exception:
                        pass
                comp_path = run_dir / "compliance.json"
                if comp_path.exists():
                    try:
                        _cj = json.loads(comp_path.read_text(encoding="utf-8"))
                        ctu = _cj.get("token_usage") or {}
                        if ctu:
                            audit_acc.add(ctu, count=ctu.get("calls", 1))
                        audit_dur.add(_cj.get("duration_seconds") or 0.0)
                    except Exception:
                        pass

                metrics = grading.get("execution_metrics") or {}
                tool_calls = metrics.get("total_tool_calls", 0)

                raw_expectations = grading.get("expectations") or []

                # Track uneval count
                uneval_count = sum(
                    1 for e in raw_expectations
                    if e.get("verdict") == "UNEVAL"
                )

                notes = []
                uns = grading.get("user_notes_summary") or {}
                notes.extend(uns.get("uncertainties") or [])
                notes.extend(uns.get("needs_review") or [])
                notes.extend(uns.get("workarounds") or [])

                if uneval_count > 0:
                    notes.append(f"UNEVAL: {uneval_count} assertion(s) could not be evaluated")

                entry_data = {
                    "pass_rate": summary.get("pass_rate", 0.0),
                    "pass_rate_programmatic": summary.get("pass_rate_programmatic"),
                    "pass_rate_llm": summary.get("pass_rate_llm"),
                    "time_seconds": time_seconds,
                    "tokens": tokens,
                }

                # eval 名从 run_debug.json 取真名(如 happy_path_basic_categorization);
                # 缺失则空,viewer fallback "Eval {id}"。旧用 skill 名(name)→ 全 eval 同名,
                # 检查点总览 / 逐项表只能显 "Eval N"。
                eval_name = ""
                debug_path = run_dir / "run_debug.json"
                if debug_path.exists():
                    try:
                        eval_name = json.loads(debug_path.read_text(encoding="utf-8")).get("eval_name") or ""
                    except (json.JSONDecodeError, OSError):
                        pass

                entry = RunResultEntry(
                    eval_id=eval_id,
                    eval_name=eval_name,
                    configuration=config_name,
                    run_number=run_number,
                    result=RunResultMetrics(
                        pass_rate=summary.get("pass_rate", 0.0),
                        passed=summary.get("passed", 0),
                        failed=summary.get("failed", 0),
                        total=summary.get("total", 0),
                        time_seconds=time_seconds,
                        tokens=tokens,
                        tool_calls=tool_calls,
                        errors=metrics.get("errors_encountered", 0),
                        # 双率透到 per-(eval,run):检查点总览 + 逐项表 3-dim 列用(与 entry_data 同源)
                        pass_rate_programmatic=summary.get("pass_rate_programmatic"),
                        pass_rate_llm=summary.get("pass_rate_llm"),
                    ),
                    expectations=raw_expectations,
                    notes=notes,
                    retry_count=retry_count,
                )
                runs.append(entry)
                configs_data[config_name].append(entry_data)

                # Track duration for outlier detection
                key = f"{eval_id}-{config_name}"
                if key not in duration_map:
                    duration_map[key] = []
                duration_map[key].append({
                    "eval_id": eval_id,
                    "eval_name": name,
                    "config": config_name,
                    "run_number": run_number,
                    "duration_seconds": time_seconds,
                })

        # Detect outliers
        for key, dur_entries in duration_map.items():
            if len(dur_entries) >= 4:
                from skill_eval.execution.runner import ABTestRunner
                timeout_anomalies = ABTestRunner.detect_timeout(
                    dur_entries, timeout_seconds=300
                )
                outlier_anomalies = ABTestRunner.detect_outliers(
                    dur_entries, threshold_multiplier=3.0
                )
                for a in timeout_anomalies:
                    anomalies.append(RunAnomaly(
                        eval_id=a["eval_id"],
                        eval_name=a.get("eval_name", ""),
                        configuration=a["config"],
                        run_number=a["run_number"],
                        anomaly_type=a["anomaly_type"],
                        detail=a["detail"],
                        value=a["duration_seconds"],
                    ))
                    # Mark corresponding run entry
                    for entry in runs:
                        if (entry.eval_id == a["eval_id"]
                                and entry.configuration == a["config"]
                                and entry.run_number == a["run_number"]):
                            entry.is_anomaly = True
                            entry.anomaly_reason = a["detail"]

                for a in outlier_anomalies:
                    anomalies.append(RunAnomaly(
                        eval_id=a["eval_id"],
                        eval_name=a.get("eval_name", ""),
                        configuration=a["config"],
                        run_number=a["run_number"],
                        anomaly_type=a["anomaly_type"],
                        detail=a["detail"],
                        value=a["duration_seconds"],
                    ))
                    for entry in runs:
                        if (entry.eval_id == a["eval_id"]
                                and entry.configuration == a["config"]
                                and entry.run_number == a["run_number"]):
                            entry.is_anomaly = True
                            entry.anomaly_reason = a["detail"]

        # Also check direct timeouts via stop_reason
        for config_dir in sorted(iteration_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            config_name = config_dir.name
            for run_dir in index_runs(config_dir, is_nested(config_dir)).values():
                debug_path = run_dir / "run_debug.json"
                if debug_path.exists():
                    try:
                        debug = json.loads(debug_path.read_text(encoding="utf-8"))
                        stop_reason = debug.get("stop_reason", "")
                        if "timed_out" in stop_reason.lower():
                            duration = debug.get("duration_ms", 0) / 1000.0
                            parent_name = run_dir.parent.name
                            eval_id = 0
                            if parent_name.startswith("eval-"):
                                try:
                                    eval_id = int(parent_name.split("-")[1])
                                except (ValueError, IndexError):
                                    pass
                            run_number = int(run_dir.name.split("-")[1])
                            anomalies.append(RunAnomaly(
                                eval_id=eval_id,
                                eval_name=debug.get("eval_name") or name,
                                configuration=config_name,
                                run_number=run_number,
                                anomaly_type="timeout",
                                detail=f"claude -p timed out after {duration:.0f}s",
                                value=duration,
                            ))
                    except Exception:
                        pass

        retried_count = sum(1 for e in runs if e.retry_count > 0)
        unresolved_count = len(anomalies)
        anomaly_report = AnomalyReport(
            anomalies=anomalies,
            total_runs=len(runs),
            anomaly_count=unresolved_count,
            retried_count=retried_count,
            unresolved_count=unresolved_count,
        )

        run_summary = self._build_summary(configs_data)

        checkpoints_data = self._load_checkpoints(iteration_dir)
        # evals_run 用 runs(authoritative):_load_checkpoints 只读**首个** checkpoints.json
        # (单 run、只见自己 eval)→ 计数偏少(如三 eval run 显 [1])。runs 涵盖所有已聚合 eval。
        eval_ids = sorted(set(r.eval_id for r in runs))

        main_benchmark = BenchmarkData(
            metadata=BenchmarkMetadata(
                skill_name=name,
                skill_path=str(skill_path),
                executor_model=real_model or self.model or "",
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                evals_run=eval_ids,
                runs_per_configuration=self._guess_runs_per_config(runs),
            ),
            runs=runs,
            run_summary=run_summary,
            anomaly_report=anomaly_report,
        )

        if checkpoints_data:
            main_benchmark = main_benchmark.model_copy(update={"notes": checkpoints_data})

        _read_end = time.perf_counter()  # read+summarize 阶段结束(_p2_start→此 = read_runs_wall)
        try:
            comparisons = run_comparisons(self, iteration_dir, sides=sides, on_usage=comparator_acc.add, on_duration=comparator_dur.add)
            if comparisons:
                # label 从 sides 透到 ComparisonSummary(默认 with/without)
                label_a = sides[0][2] if sides else "with"
                label_b = sides[1][2] if sides else "without"
                main_benchmark = main_benchmark.model_copy(
                    update={
                        "comparisons": comparisons,
                        "comparison_summary": ComparisonSummary.from_comparisons(comparisons, label_a, label_b),
                        "metadata": main_benchmark.metadata.model_copy(update={"comparator_model": real_model or self.model or "default"}),
                    }
                )
        except Exception as exc:
            print(f"Warning: comparison step failed ({exc}), skipping", file=__import__("sys").stderr)
        _cmp_end = time.perf_counter()  # comparator 阶段墙钟(_read_end→此)

        try:
            per_side_cal = _collect_per_side_calibration(iteration_dir, sides)
            # 与 comparison_summary 对称:至少一侧有报告才钉 summary,否则留 None
            # (无 calibration 数据 vs calibration 数据为空,前者更准确)
            if any(per_side_cal.values()):
                cal_summary = CalibrationSummary.from_per_side(per_side_cal)
                main_benchmark = main_benchmark.model_copy(update={"calibration_summary": cal_summary})
        except Exception as exc:
            print(f"Warning: calibration summary failed ({exc}), skipping", file=__import__("sys").stderr)

        _an_start = time.perf_counter()
        try:
            analysis_notes, analysis_result = run_analysis(self, iteration_dir, main_benchmark, on_usage=analyzer_acc.add, on_duration=analyzer_dur.add)
            if analysis_notes:
                existing = list(main_benchmark.notes or [])
                existing.extend(analysis_notes)
                main_benchmark = main_benchmark.model_copy(update={"notes": existing})
            if analysis_result:
                main_benchmark = main_benchmark.model_copy(
                    update={
                        "analysis": analysis_result,
                        "metadata": main_benchmark.metadata.model_copy(update={"analyzer_model": real_model or self.model or "default"}),
                    }
                )
        except Exception as exc:
            print(f"Warning: analysis step failed ({exc}), skipping", file=__import__("sys").stderr)
        _an_end = time.perf_counter()  # analyzer 阶段墙钟

        # Phase 2: 汇总 → llm_token_usage(scoring 合计 + grader/comparator/analyzer 分计 + audit/executor/total)
        # 只在 snapshot 非空时 add(空 snapshot = 无调用,add({}) 会假计数)
        grader_snapshot = grader_acc.snapshot()
        comparator_snapshot = comparator_acc.snapshot()
        analyzer_snapshot = analyzer_acc.snapshot()
        scoring_total = TokenUsageAccumulator()
        for snap in (comparator_snapshot, analyzer_snapshot, grader_snapshot):
            if snap:
                scoring_total.add(snap, count=snap.get("calls", 1))
        audit_snapshot = audit_acc.snapshot()
        total_acc = TokenUsageAccumulator()
        for snap in (scoring_total.snapshot(), audit_snapshot):
            if snap:
                total_acc.add(snap, count=snap.get("calls", 1))
        if executor_total:
            # executor 是 cc 跑任务的 token,非评分 LLM 调用 → 累 input 但不计 calls(count=0)
            total_acc.add({"input_tokens": executor_total, "output_tokens": 0}, count=0)
        # 墙钟分解:phase1 含 setup→run_ab_test(调用方传),phase2 = aggregate 自身。
        # 各自 round 到 3 位后再加 → total 严格 == phase1_wall + phase2_wall(免 rounding 抖)。
        # 其后的 static/vet/report 生成是写后步骤、自指不入 total(viewer 注明)。
        p1_wall = round(phase1_wall or 0.0, 3)
        p2_wall = round(time.perf_counter() - _p2_start, 3)
        main_benchmark = main_benchmark.model_copy(update={
            "llm_token_usage": {
                "scoring": scoring_total.snapshot(),  # grader+comparator+analyzer 合计(向后兼容)
                "grader": grader_snapshot,
                "comparator": comparator_snapshot,
                "analyzer": analyzer_snapshot,
                "audit": audit_snapshot,
                "executor": {"input_tokens": executor_total, "output_tokens": 0, "calls": 0},
                "total": total_acc.snapshot(),
            },
            "llm_duration_seconds": {
                "phase1_wall": p1_wall,
                "phase2_wall": p2_wall,
                # phase2 子墙钟(整段计时;与分列累计和是两维度):read+summarize / comparator / analyzer
                "read_runs_wall": round(_read_end - _p2_start, 3),
                "comparator_wall": round(_cmp_end - _read_end, 3),
                "analyzer_wall": round(_an_end - _an_start, 3),
                # 分列=各类 LLM 调用时长加总(并发叠加,可 ≥ 墙钟,与墙钟分解是两个维度)
                "scoring": round(comparator_dur.snapshot() + analyzer_dur.snapshot() + grader_dur.snapshot(), 3),
                "grader": round(grader_dur.snapshot(), 3),
                "comparator": round(comparator_dur.snapshot(), 3),
                "analyzer": round(analyzer_dur.snapshot(), 3),
                "audit": round(audit_dur.snapshot(), 3),
                "executor": round(executor_dur, 3),
                "total": round(p1_wall + p2_wall, 3),  # = p1_wall + p2_wall(两者已 3 位,和恰 3 位)
            },
        })

        return main_benchmark

    def _load_checkpoints(self, iteration_dir: Path) -> list[dict]:
        for config_dir in sorted(iteration_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            for run_dir in index_runs(config_dir, is_nested(config_dir)).values():
                cp_path = run_dir / "checkpoints.json"
                if cp_path.exists():
                    try:
                        return json.loads(cp_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
        return []

    def _build_summary(self, configs_data: dict[str, list[dict]]) -> dict:
        run_summary: dict[str, dict] = {}
        configs = list(configs_data.keys())

        for config in configs:
            entries = configs_data[config]
            if not entries:
                run_summary[config] = {
                    "pass_rate": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                    "pass_rate_programmatic": None,
                    "pass_rate_llm": None,
                    "time_seconds": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                    "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0},
                }
                continue

            pr = calculate_stats([e["pass_rate"] for e in entries])
            ts = calculate_stats([e["time_seconds"] for e in entries])
            tk = calculate_stats([float(e["tokens"]) for e in entries])
            # 双率:过滤 None(该 run 无该类 expectation),与 compliance 双率同构
            prog_vals = [e["pass_rate_programmatic"] for e in entries if e.get("pass_rate_programmatic") is not None]
            llm_vals = [e["pass_rate_llm"] for e in entries if e.get("pass_rate_llm") is not None]

            run_summary[config] = {
                "pass_rate": pr.model_dump(),
                "pass_rate_programmatic": calculate_stats(prog_vals).model_dump() if prog_vals else None,
                "pass_rate_llm": calculate_stats(llm_vals).model_dump() if llm_vals else None,
                "time_seconds": ts.model_dump(),
                "tokens": tk.model_dump(),
            }

        if len(configs) >= 2:
            a = run_summary.get(configs[0], {})
            b = run_summary.get(configs[1], {})
            a_pr = a.get("pass_rate", {}).get("mean", 0)
            b_pr = b.get("pass_rate", {}).get("mean", 0)
            dpr = a_pr - b_pr
            dts = a.get("time_seconds", {}).get("mean", 0) - b.get("time_seconds", {}).get("mean", 0)
            dtk = a.get("tokens", {}).get("mean", 0) - b.get("tokens", {}).get("mean", 0)

            def _dual_delta(field):
                # 任一 config 无该类 expectation → delta 该项 None(不报假 0)
                av = a.get(field)
                bv = b.get(field)
                if not av or not bv:
                    return None
                return f"{av.get('mean', 0) - bv.get('mean', 0):+.2f}"

            run_summary["delta"] = {
                "pass_rate": f"{dpr:+.2f}",
                "pass_rate_normalized_gain": f"{normalized_gain(b_pr, a_pr):+.2f}",
                "pass_rate_programmatic": _dual_delta("pass_rate_programmatic"),
                "pass_rate_llm": _dual_delta("pass_rate_llm"),
                "time_seconds": f"{dts:+.1f}",
                "tokens": f"{dtk:+.0f}",
            }

        return run_summary

    def _guess_runs_per_config(self, runs: list[RunResultEntry]) -> int:
        if not runs:
            return 0
        configs: dict[str, set[int]] = {}
        for r in runs:
            configs.setdefault(r.configuration, set()).add(r.run_number)
        return min(len(v) for v in configs.values()) if configs else 0
