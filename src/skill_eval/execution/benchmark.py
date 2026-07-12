"""Benchmark aggregator — merges run results into benchmark.json."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from skill_eval.models import (
    AnomalyReport,
    BenchmarkData,
    BenchmarkMetadata,
    ComparisonSummary,
    RunAnomaly,
    RunResultEntry,
    RunResultMetrics,
    StatsValue,
)
from skill_eval.parser import parse_skill_md
from skill_eval.execution.comparison_suite import run_comparisons
from skill_eval.execution.analysis_suite import run_analysis
from skill_eval.execution.run_paths import index_runs, is_nested


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


class BenchmarkAggregator:
    def __init__(self, model: str | None = None, timeout: int = 120, enable_llm: bool = True):
        self.model = model
        self.timeout = timeout
        # 盲比较只服务于主观题;programmatic-only(enable_llm=False)时 run_comparisons 跳过
        self.enable_llm = enable_llm

    def aggregate(self, iteration_dir: Path, skill_path: Path = Path(), sides: list | None = None) -> BenchmarkData:
        import sys
        sys.stderr.write("aggregate: reading run results...\n")
        sys.stderr.flush()

        name = ""
        if skill_path and (skill_path / "SKILL.md").exists():
            name, _, _, _, _ = parse_skill_md(skill_path)

        runs: list[RunResultEntry] = []
        configs_data: dict[str, list[dict]] = {}
        anomalies: list[RunAnomaly] = []
        duration_map: dict[str, list[dict]] = {}

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

                entry = RunResultEntry(
                    eval_id=eval_id,
                    eval_name=name,
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
                                eval_name=name,
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
        eval_ids = sorted(set(cp.get("eval_id", 0) for cp in checkpoints_data)) if checkpoints_data else sorted(set(r.eval_id for r in runs))

        main_benchmark = BenchmarkData(
            metadata=BenchmarkMetadata(
                skill_name=name,
                skill_path=str(skill_path),
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

        try:
            comparisons = run_comparisons(self, iteration_dir, sides=sides)
            if comparisons:
                # label 从 sides 透到 ComparisonSummary(默认 with/without)
                label_a = sides[0][2] if sides else "with"
                label_b = sides[1][2] if sides else "without"
                main_benchmark = main_benchmark.model_copy(
                    update={
                        "comparisons": comparisons,
                        "comparison_summary": ComparisonSummary.from_comparisons(comparisons, label_a, label_b),
                        "metadata": main_benchmark.metadata.model_copy(update={"comparator_model": self.model or "default"}),
                    }
                )
        except Exception as exc:
            print(f"Warning: comparison step failed ({exc}), skipping", file=__import__("sys").stderr)

        try:
            analysis_notes, analysis_result = run_analysis(self, iteration_dir, main_benchmark)
            if analysis_notes:
                existing = list(main_benchmark.notes or [])
                existing.extend(analysis_notes)
                main_benchmark = main_benchmark.model_copy(update={"notes": existing})
            if analysis_result:
                main_benchmark = main_benchmark.model_copy(
                    update={
                        "analysis": analysis_result,
                        "metadata": main_benchmark.metadata.model_copy(update={"analyzer_model": self.model or "default"}),
                    }
                )
        except Exception as exc:
            print(f"Warning: analysis step failed ({exc}), skipping", file=__import__("sys").stderr)

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
            dpr = a.get("pass_rate", {}).get("mean", 0) - b.get("pass_rate", {}).get("mean", 0)
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
