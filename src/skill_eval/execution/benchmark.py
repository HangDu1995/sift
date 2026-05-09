"""Benchmark aggregator — merges run results into benchmark.json."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from skill_eval.models import (
    BenchmarkData,
    BenchmarkMetadata,
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
    def aggregate(self, iteration_dir: Path, skill_path: Path = Path()) -> BenchmarkData:
        name = ""
        if skill_path and (skill_path / "SKILL.md").exists():
            name, _, _, _, _ = parse_skill_md(skill_path)

        runs: list[RunResultEntry] = []
        configs_data: dict[str, list[dict]] = {}

        for config_dir in sorted(iteration_dir.iterdir()):
            if not config_dir.is_dir() or not list(config_dir.glob("run-*")):
                continue
            config_name = config_dir.name
            if config_name not in configs_data:
                configs_data[config_name] = []

            for run_dir in sorted(config_dir.glob("run-*")):
                run_number = int(run_dir.name.split("-")[1])
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

                notes = []
                uns = grading.get("user_notes_summary") or {}
                notes.extend(uns.get("uncertainties") or [])
                notes.extend(uns.get("needs_review") or [])
                notes.extend(uns.get("workarounds") or [])

                entry_data = {
                    "pass_rate": summary.get("pass_rate", 0.0),
                    "time_seconds": time_seconds,
                    "tokens": tokens,
                }

                entry = RunResultEntry(
                    eval_id=0,
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
                )
                runs.append(entry)
                configs_data[config_name].append(entry_data)

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
        )

        checkpoints_data = self._load_checkpoints(iteration_dir)
        if checkpoints_data:
            main_benchmark = main_benchmark.model_copy(update={"notes": checkpoints_data})

        return main_benchmark

    def _load_checkpoints(self, iteration_dir: Path) -> list[dict]:
        for config_dir in sorted(iteration_dir.iterdir()):
            if not config_dir.is_dir():
                continue
            for run_dir in sorted(config_dir.glob("run-*")):
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
