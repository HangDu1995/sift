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
    skip_compliance: bool = False,
    refine_instructions: bool = True,
    output_dir: Path = Path("output"),
    fmt: str = "table",
    eval_samples: int | float | None = None,
    eval_ids: list[int] | None = None,
    max_retries: int = 2,
    skill_b: Path | None = None,
    label_a: str | None = None,
    label_b: str | None = None,
) -> tuple:
    from skill_eval.execution.runner import ABTestRunner, build_sides
    from skill_eval.execution.benchmark import BenchmarkAggregator

    if eval_ids and eval_samples:
        raise SkillEvalError("--eval-ids 和 --eval-samples 互斥(指定 id 就不再随机抽样)")

    # sides spec 单一事实源:skill_b 给定 → v1/v2 两 skill;None → 默认 with/without。
    sides = build_sides(skill_path, skill_b, label_a, label_b)

    lc = _log_console(fmt)
    total_evals = len(eval_set.evals)
    actual_evals = total_evals
    selected = ""
    if eval_ids:
        actual_evals = len(eval_ids)
        selected = f" (by id: {','.join(map(str, eval_ids))} = {actual_evals}/{total_evals})"
    sampled = ""
    if eval_samples:
        if isinstance(eval_samples, float) and eval_samples <= 1.0:
            actual_evals = max(1, int(total_evals * eval_samples))
            sampled = f" (sampled {int(eval_samples * 100)}% = {actual_evals}/{total_evals})"
        else:
            actual_evals = min(int(eval_samples), total_evals)
            sampled = f" (sampled {actual_evals}/{total_evals})"
    lc.print(f"Running A/B test: {eval_set.skill_name} ({total_evals} evals{selected}{sampled} × {runs} runs)")

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
        skip_compliance=skip_compliance,
        refine_instructions=refine_instructions,
        eval_samples=eval_samples,
        eval_ids=eval_ids,
        per_eval_timeout=timeout,
        min_runs_for_stats=max(3, runs),
        max_retries_per_eval=max_retries,
    )

    progress_state = _ProgressState(actual_evals, runs)
    on_progress = _make_progress_callback(progress_state)

    try:
        ab = ABTestRunner(run_config, scoring_config, on_progress=on_progress)
        iteration_dir = ab.run_ab_test(skill_path, eval_set, sides=sides)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    agg = BenchmarkAggregator(model=model, enable_llm=scoring_config.enable_llm)
    try:
        benchmark = agg.aggregate(iteration_dir, skill_path, sides=sides)
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
    analysis = getattr(benchmark, "analysis", None)
    cs = getattr(benchmark, "comparison_summary", None)  # 主观侧主位

    if fmt == "json":
        console.print(json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        # side key 配置无关地从 run_summary 取(with_skill/without_skill 或 v1/v2)
        side_keys = [k for k in summary.keys() if k != "delta"]
        a_mean = (summary.get(side_keys[0], {}).get("pass_rate", {}).get("mean", 0) * 100) if side_keys else 0
        b_mean = (summary.get(side_keys[1], {}).get("pass_rate", {}).get("mean", 0) * 100) if len(side_keys) > 1 else 0
        pr_delta = delta.get("pass_rate", "—")
        la = cs.label_a if cs else (side_keys[0] if side_keys else "side A")
        lb = cs.label_b if cs else (side_keys[1] if len(side_keys) > 1 else "side B")

        console.print(f"\n[bold]Results:[/bold]")

        # 1. Quality — blind comparison (主观侧主位)
        if cs and cs.valid > 0:
            console.print(f"  [green]Quality:[/green]   {la} 盲评胜率 {cs.win_rate*100:.0f}% ({cs.a_wins}/{cs.valid})")
        else:
            console.print(f"  Quality:   (add --model for blind comparison)")

        # 2. Pass rate — mechanical assertions (客观侧)
        delta_color = ""
        if isinstance(pr_delta, (int, float)) and pr_delta < 0:
            delta_color = "red"
        elif isinstance(pr_delta, str) and pr_delta.startswith("-"):
            delta_color = "red"
        delta_str = f"[{delta_color}]({pr_delta})[/{delta_color}]" if delta_color else f"({pr_delta})"
        console.print(f"  Pass rate:  {la} {a_mean:.0f}% vs {lb} {b_mean:.0f}% {delta_str}")

        # 3. Analysis insights
        if analysis and isinstance(analysis, dict):
            strengths = analysis.get("winner_strengths", [])
            if strengths:
                console.print(f"\n[bold]Key strengths:[/bold]")
                for s in strengths[:2]:
                    console.print(f"  • {s}")

        console.print(f"\nBenchmark saved to: {benchmark_path}")

    # 缓存复用提示:全跳过 = 输入未变、结果可信复用;部分跳过也明说。避免"进度条瞬间 100%"的困惑。
    for line in _cache_notice_lines(progress_state, output_dir):
        console.print(line)

    return benchmark, iteration_dir


def _make_progress_callback(progress_state):
    """on_progress 闭包:把 runner 的进度事件转给 progress_state.update。

    参数必须逐个对齐 ``update(eval_name, config_name, eval_idx, total_evals, completed,
    total, grading, run_duration, run_output, …)``——曾漏传 ``total_evals`` 致整体左移:
    update.grading 收到 run_duration(float)→ completion 渲染 grading.summary 崩,被 future
    兜底吞成 per-slot "Unhandled error",live bar 中途崩留残影(skip 场景进度条错乱的真凶)。
    抽成工厂便于对参数对齐做单测。
    """
    def on_progress(eval_name, config_name, eval_idx, total_evals, completed, total, grading, run_duration=0.0, run_output=None, phase="", substep=""):
        progress_state.update(eval_name, config_name, eval_idx, total_evals, completed, total, grading, run_duration, run_output, phase=phase, substep=substep)
    return on_progress


def _cache_notice_lines(progress_state, output_dir) -> list[str]:
    """据 progress_state.skipped 产缓存复用提示行(空表 = 无需提示)。抽出为纯函数便于测。"""
    if progress_state.skipped <= 0:
        return []
    if progress_state.skipped == progress_state.total_runs:
        return [
            f"[yellow]⚠️ 全部 {progress_state.total_runs} 个槽命中既有结果,未重跑"
            f"(skill/eval/模型 较上次未变)。[/yellow]",
            f"[yellow]   改了 skill/eval 想重跑:换 -o 或清空 {output_dir}。[/yellow]",
        ]
    return [
        f"[dim]缓存:{progress_state.skipped}/{progress_state.total_runs} "
        f"个槽复用既有结果未重跑(内容未变)。[/dim]",
    ]
