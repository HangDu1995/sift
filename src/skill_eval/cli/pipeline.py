from __future__ import annotations

import json
from pathlib import Path

import typer

from skill_eval.cli.helpers import _log_console
from skill_eval.cli.main import console
from skill_eval.cli.progress import _ProgressState
from skill_eval.errors import SkillEvalError
from skill_eval.models import RunConfig, ScoringConfig


def _run_ab_eval(
    skill_path: Path,
    eval_set,
    runs: int,
    model: str | None,
    workers: int,
    timeout: int,
    max_turns: int,
    scoring: str,
    no_calibration: bool,
    output_dir: Path,
    fmt: str = "table",
    eval_samples: int | float | None = None,
    per_eval_mode: bool = True,
) -> tuple:
    from skill_eval.execution.runner import ABTestRunner
    from skill_eval.execution.benchmark import BenchmarkAggregator

    lc = _log_console(fmt)
    total_evals = len(eval_set.evals)
    sampled = ""
    if eval_samples:
        if isinstance(eval_samples, float) and eval_samples <= 1.0:
            sampled = f" (sampled {int(eval_samples * 100)}%)"
        else:
            sampled = f" (sampled {eval_samples}/{total_evals})"
    lc.print(f"Running A/B test: {eval_set.skill_name} ({total_evals} evals{sampled} × {runs} runs, {'per-eval' if per_eval_mode else 'merged'} mode)")

    scoring_config = ScoringConfig(
        enable_programmatic=scoring in ("hybrid", "programmatic"),
        enable_llm=scoring in ("hybrid", "llm"),
        llm_model=model,
        llm_timeout=timeout,
    )
    run_config = RunConfig(
        runs_per_config=runs,
        model=model,
        timeout_seconds=timeout,
        max_workers=workers,
        max_turns=max_turns,
        output_dir=output_dir,
        skip_calibration=no_calibration,
        eval_samples=eval_samples,
        per_eval_mode=per_eval_mode,
        per_eval_timeout=timeout,
        min_runs_for_stats=max(3, runs),
    )

    progress_state = _ProgressState(len(eval_set.evals), runs)

    def on_progress(eval_name, config_name, eval_idx, total_evals, completed, total, grading, run_duration=0.0, run_output=None, phase="", substep=""):
        progress_state.update(eval_name, config_name, eval_idx, completed, total, grading, run_duration, run_output, phase=phase, substep=substep)

    try:
        ab = ABTestRunner(run_config, scoring_config, on_progress=on_progress)
        iteration_dir = ab.run_ab_test(skill_path, eval_set)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    agg = BenchmarkAggregator(model=model)
    try:
        benchmark = agg.aggregate(iteration_dir, skill_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Benchmark aggregation failed: {exc}")
        console.print(f"  iteration_dir: {iteration_dir}")
        configs = [d.name for d in iteration_dir.iterdir() if d.is_dir()]
        console.print(f"  config dirs found: {configs}")
        for c in configs:
            cd = iteration_dir / c
            runs_direct = list(cd.glob("run-*"))
            runs_nested = list(cd.glob("eval-*/run-*"))
            console.print(f"    {c}/: {len(runs_direct)} direct runs, {len(runs_nested)} nested runs")
        raise typer.Exit(code=1)

    benchmark_path = iteration_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = benchmark.run_summary
    delta = summary.get("delta", {})

    if fmt == "json":
        console.print(json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        ws = summary.get("with_skill", {}).get("pass_rate", {})
        wos = summary.get("without_skill", {}).get("pass_rate", {})
        console.print(f"\n[bold]Results:[/bold]")
        console.print(f"  with_skill:    {ws.get('mean', 0)*100:.0f}% pass rate")
        console.print(f"  without_skill: {wos.get('mean', 0)*100:.0f}% pass rate")
        console.print(f"  delta:         {delta.get('pass_rate', '—')}")
        console.print(f"\nBenchmark saved to: {benchmark_path}")

    pr = delta.get("pass_rate", "+0.00")
    if isinstance(pr, str) and pr.startswith("-"):
        raise typer.Exit(code=1)
    elif isinstance(pr, (int, float)) and pr < 0:
        raise typer.Exit(code=1)

    return benchmark, iteration_dir
