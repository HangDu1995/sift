"""Benchmark aggregator — merges run results into benchmark.json."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from skill_eval.models import (
    AnalysisResult,
    AnomalyReport,
    AssertionVerdict,
    BenchmarkData,
    BenchmarkMetadata,
    ComparisonResult,
    RunAnomaly,
    RunResultEntry,
    RunResultMetrics,
    StatsValue,
)
from skill_eval.parser import parse_skill_md


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
    def __init__(self, model: str | None = None, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def aggregate(self, iteration_dir: Path, skill_path: Path = Path()) -> BenchmarkData:
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

            run_dirs = sorted(config_dir.glob("run-*"))
            nested = sorted(p for p in config_dir.glob("eval-*/run-*") if p.is_dir())
            if nested:
                run_dirs = nested
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
            nested = sorted(p for p in config_dir.glob("eval-*/run-*") if p.is_dir())
            for run_dir in nested:
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
            comparisons = self._run_comparisons(iteration_dir)
            if comparisons:
                main_benchmark = main_benchmark.model_copy(
                    update={
                        "comparisons": comparisons,
                        "metadata": main_benchmark.metadata.model_copy(update={"comparator_model": self.model or "default"}),
                    }
                )
        except Exception as exc:
            print(f"Warning: comparison step failed ({exc}), skipping", file=__import__("sys").stderr)

        try:
            analysis_notes, analysis_result = self._run_analysis(iteration_dir, main_benchmark)
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
            run_dirs = sorted(config_dir.glob("run-*"))
            nested = sorted(p for p in config_dir.glob("eval-*/run-*") if p.is_dir())
            if nested:
                run_dirs = nested
            for run_dir in run_dirs:
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
                    "time_seconds": {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0},
                    "tokens": {"mean": 0, "stddev": 0, "min": 0, "max": 0},
                }
                continue

            pr = calculate_stats([e["pass_rate"] for e in entries])
            ts = calculate_stats([e["time_seconds"] for e in entries])
            tk = calculate_stats([float(e["tokens"]) for e in entries])

            run_summary[config] = {
                "pass_rate": pr.model_dump(),
                "time_seconds": ts.model_dump(),
                "tokens": tk.model_dump(),
            }

        if len(configs) >= 2:
            a = run_summary.get(configs[0], {})
            b = run_summary.get(configs[1], {})
            dpr = a.get("pass_rate", {}).get("mean", 0) - b.get("pass_rate", {}).get("mean", 0)
            dts = a.get("time_seconds", {}).get("mean", 0) - b.get("time_seconds", {}).get("mean", 0)
            dtk = a.get("tokens", {}).get("mean", 0) - b.get("tokens", {}).get("mean", 0)
            run_summary["delta"] = {
                "pass_rate": f"{dpr:+.2f}",
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

    def _run_comparisons(self, iteration_dir: Path) -> list:
        import sys, time, threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from skill_eval.scoring.comparator import BlindComparator

        with_skill_dir = iteration_dir / "with_skill"
        without_skill_dir = iteration_dir / "without_skill"
        if not with_skill_dir.is_dir() or not without_skill_dir.is_dir():
            return []

        comparator = BlindComparator(model=self.model, timeout=self.timeout)

        ws_runs = sorted(p for p in with_skill_dir.glob("run-*") if p.is_dir())
        ws_nested = sorted(p for p in with_skill_dir.glob("eval-*/run-*") if p.is_dir())
        if ws_nested:
            ws_runs = ws_nested
        wos_runs = sorted(p for p in without_skill_dir.glob("run-*") if p.is_dir())
        wos_nested = sorted(p for p in without_skill_dir.glob("eval-*/run-*") if p.is_dir())
        if wos_nested:
            wos_runs = wos_nested

        pairs = list(zip(ws_runs, wos_runs))
        total = len(pairs)
        if total == 0:
            return []

        completed_count = [0]
        failures = [0]
        lock = threading.Lock()
        start_time = time.time()

        def _progress_line():
            with lock:
                c = completed_count[0]
            pct = c / total * 100 if total else 0
            bar = self._progress_bar(c, total, 20)
            elapsed = time.time() - start_time
            eta_str = ""
            if c > 0:
                eta_s = (elapsed / c) * (total - c)
                eta_str = f" ETA {eta_s/60:.0f}m{eta_s%60:02.0f}s"
            return f"\r[{c}/{total}] comparison {bar} {pct:.0f}%{eta_str}"

        def _do_comparison(idx, ws_run, wos_run):
            ws_output = self._collect_run_output(ws_run)
            wos_output = self._collect_run_output(wos_run)
            ws_grading = self._load_grading(ws_run)

            merged_prompt_path = iteration_dir / "merged_prompt.txt"
            eval_prompt = merged_prompt_path.read_text(encoding="utf-8")[:2000] if merged_prompt_path.exists() else ""

            expectations = []
            if ws_grading:
                expectations = [e.get("text", "") for e in ws_grading.get("expectations", []) if e.get("text")]

            result = self._compare_with_retry(comparator, ws_output, wos_output, eval_prompt, expectations)
            if result is None:
                with lock:
                    failures[0] += 1
                result = ComparisonResult(winner="TIE", reasoning="comparison failed after retries")

            comp_path = ws_run.parent.parent / f"comparison-run{ws_run.name.split('-')[1]}.json"
            comp_path.write_text(
                json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            with lock:
                completed_count[0] += 1
            sys.stderr.write(_progress_line())
            sys.stderr.flush()
            return (idx, result)

        comparisons_dict: dict[int, ComparisonResult] = {}
        max_workers = min(total, 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            sys.stderr.write(_progress_line())
            sys.stderr.flush()
            futures = {pool.submit(_do_comparison, i, ws, wos): i for i, (ws, wos) in enumerate(pairs)}
            for future in as_completed(futures):
                idx, result = future.result()
                comparisons_dict[idx] = result

        comparisons = [comparisons_dict[i] for i in sorted(comparisons_dict)]
        status = f"\r[{total}/{total}] comparison done ({time.time()-start_time:.0f}s)"
        if failures[0]:
            status += f", {failures[0]} failed"
        sys.stderr.write(status + "\n")
        sys.stderr.flush()
        return comparisons

    def _compare_with_retry(self, comparator, ws_output, wos_output, eval_prompt, expectations, max_retries=3):
        for attempt in range(max_retries):
            try:
                return comparator.compare(
                    output_a=ws_output,
                    output_b=wos_output,
                    eval_prompt=eval_prompt,
                    expectations=expectations if expectations else None,
                )
            except Exception:
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2 ** attempt)
        return None

    def _progress_bar(self, current, total, width):
        pct = min(current / total, 1.0) if total else 0
        filled = int(width * pct)
        bar = "\u2588" * filled + "\u2591" * (width - filled)
        return f" {bar} {pct*100:.0f}%"

    def _run_analysis(self, iteration_dir: Path, benchmark: BenchmarkData) -> tuple[list[str], object | None]:
        import sys
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from skill_eval.scoring.analyzer import PosthocAnalyzer

        if not benchmark.runs:
            return [], None

        sys.stderr.write("analysis: evaluating benchmark...\n")
        sys.stderr.flush()

        analyzer = PosthocAnalyzer(model=self.model, timeout=self.timeout)
        benchmark_json = json.dumps(benchmark.model_dump(mode="json"), ensure_ascii=False)[:12000]

        notes = None
        for attempt in range(3):
            try:
                notes = analyzer.analyze_benchmark(benchmark_json, benchmark.metadata.skill_name)
                break
            except Exception:
                if attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
        if notes:
            notes_path = iteration_dir / "analysis-notes.json"
            notes_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")

        comp_analysis = None
        if benchmark.comparisons:
            for attempt in range(3):
                try:
                    comp_analysis = self._run_comparison_analysis(iteration_dir, benchmark, analyzer)
                    break
                except Exception:
                    if attempt < 2:
                        import time
                        time.sleep(2 ** attempt)
            if comp_analysis:
                analysis_path = iteration_dir / "analysis-result.json"
                analysis_path.write_text(
                    json.dumps(comp_analysis.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        sys.stderr.write("analysis: done\n")
        sys.stderr.flush()

        return notes, comp_analysis

    def _run_comparison_analysis(self, iteration_dir: Path, benchmark: BenchmarkData, analyzer) -> AnalysisResult | None:
        from skill_eval.parser import parse_skill_md

        skill_path = Path(benchmark.metadata.skill_path) if benchmark.metadata.skill_path else None
        if not skill_path or not (skill_path / "SKILL.md").exists():
            return None

        _, _, _, _, skill_body = parse_skill_md(skill_path)
        skill_content = skill_body[:4000]

        if not benchmark.comparisons:
            return None

        first_comp = benchmark.comparisons[0]
        winner_label = first_comp.winner

        with_skill_dir = iteration_dir / "with_skill"
        without_skill_dir = iteration_dir / "without_skill"
        ws_runs = sorted(with_skill_dir.glob("run-*"))
        ws_nested = sorted(with_skill_dir.glob("eval-*/run-*"))
        if ws_nested:
            ws_runs = ws_nested
        wos_runs = sorted(without_skill_dir.glob("run-*"))
        wos_nested = sorted(without_skill_dir.glob("eval-*/run-*"))
        if wos_nested:
            wos_runs = wos_nested
        if not ws_runs or not wos_runs:
            return None

        ws_transcript = self._load_transcript(ws_runs[0])
        wos_transcript = self._load_transcript(wos_runs[0])

        if winner_label == "A":
            winner_skill, loser_skill = skill_content, "(no skill)"
            winner_trans, loser_trans = ws_transcript, wos_transcript
        elif winner_label == "B":
            winner_skill, loser_skill = "(no skill)", skill_content
            winner_trans, loser_trans = wos_transcript, ws_transcript
        else:
            return None

        return analyzer.analyze_comparison(
            winner=winner_label,
            winner_skill=winner_skill,
            loser_skill=loser_skill,
            comparison_reasoning=first_comp.reasoning,
            winner_transcript=winner_trans,
            loser_transcript=loser_trans,
        )

    def _collect_run_output(self, run_dir: Path) -> str:
        outputs_dir = run_dir / "outputs"
        if not outputs_dir.is_dir():
            grading = self._load_grading(run_dir)
            if grading:
                return json.dumps(grading, indent=2, ensure_ascii=False)[:4000]
            return ""
        parts = []
        for f in sorted(outputs_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if len(content) > 2000:
                        content = content[:2000] + "\n... (truncated)"
                    parts.append(f"=== {f.name} ===\n{content}")
                except Exception:
                    parts.append(f"=== {f.name} === (binary)")
        return "\n\n".join(parts) if parts else ""

    def _load_grading(self, run_dir: Path) -> dict | None:
        grading_path = run_dir / "grading.json"
        if grading_path.exists():
            try:
                return json.loads(grading_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def _load_transcript(self, run_dir: Path) -> str:
        transcript_path = run_dir / "transcript.md"
        if transcript_path.exists():
            return transcript_path.read_text(encoding="utf-8", errors="replace")[:4000]
        debug_path = run_dir / "run_debug.json"
        if debug_path.exists():
            try:
                debug = json.loads(debug_path.read_text(encoding="utf-8"))
                return debug.get("transcript_preview", "")[:4000]
            except Exception:
                pass
        return ""
