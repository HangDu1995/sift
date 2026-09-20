from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import typer

from sift.cli.helpers import _log_console
from sift.cli.main import console
from sift.cli.progress import _ProgressState
from sift.errors import SiftError
from sift.models import RunConfig, ScoringConfig


class PassRateSide(NamedTuple):
    """一侧评测的显示名和通过率百分数。"""

    label: str
    mean: float


class RunHeader(NamedTuple):
    """启动提示所需的运行范围及次数。"""

    num_sides: int
    skill_name: str
    total_evals: int
    selected: str
    sampled: str
    runs: int


def format_failure_triage(runs) -> list[str]:
    """从 runs 聚合失败根因为待打印行(纯函数,可单测;镜像 _format_run_summary 模式)。

    无 error run → [](不打印空块)。有 → 标题 + 按 root_cause 计数(众数优先)+ 对应建议。
    纯诊断展示,不进任何判决。
    """
    triaged = [(r.error_root_cause, r.error_suggestion) for r in runs if r.error_root_cause]
    if not triaged:
        return []
    cause_counts = Counter(cause for cause, _ in triaged)
    suggestion_of = {cause: sug for cause, sug in triaged}
    lines = ["[bold]失败根因汇总:[/bold]"]
    for cause, count in cause_counts.most_common():
        lines.append(f"  {count}× {cause} — {suggestion_of[cause]}")
    return lines


def _format_pass_rate_line(side_keys, side_a: PassRateSide, side_b: PassRateSide, pr_delta) -> str:
    """格式化 Pass rate 行(纯函数,可单测)。

    agent 评测(len(side_keys)<2,绝对质量)→ 只报绝对 pass_rate,无 vs/delta(无 A/B 对照)。
    两臂 → ``A vs B + delta``(负 delta 标红)。
    """
    la, a_mean = side_a
    lb, b_mean = side_b
    if len(side_keys) < 2:
        return f"  Pass rate:  {la} {a_mean:.0f}%"
    delta_color = ""
    if isinstance(pr_delta, (int, float)) and pr_delta < 0:
        delta_color = "red"
    elif isinstance(pr_delta, str) and pr_delta.startswith("-"):
        delta_color = "red"
    delta_str = f"[{delta_color}]({pr_delta})[/{delta_color}]" if delta_color else f"({pr_delta})"
    return f"  Pass rate:  {la} {a_mean:.0f}% vs {lb} {b_mean:.0f}% {delta_str}"


def _format_run_header(header: RunHeader) -> str:
    """格式化 run 启动头行(纯函数,可单测)。

    agent 评测(num_sides<2,绝对质量)→ ``Running agent eval``(无 A/B 对照);
    两臂 → ``Running A/B test``。措辞与 _format_pass_rate_line 的单臂判定一致。
    """
    num_sides, skill_name, total_evals, selected, sampled, runs = header
    verb = "agent eval" if num_sides < 2 else "A/B test"
    return f"Running {verb}: {skill_name} ({total_evals} evals{selected}{sampled} × {runs} runs)"


class _EvaluationPresentation(NamedTuple):
    """统一入口传给执行阶段的展示、选题和命令起始时间。"""

    sides: list
    actual_evals: int
    selection: object
    fmt: str
    wall_start: float


class EvaluationOptions(NamedTuple):
    """CLI 的比较对象、变更选择和展示设置；运行参数由 RunConfig 承担。"""

    fmt: str = "table"
    changed_from: Path | None = None
    skill_b: Path | None = None
    label_a: str | None = None
    label_b: str | None = None


def _run_ab_eval(skill_path: Path, eval_set, run_config: RunConfig, scoring: str, options: EvaluationOptions) -> tuple:
    runs = run_config.runs_per_config
    model = run_config.model
    timeout = run_config.timeout_seconds
    eval_samples = run_config.eval_samples
    eval_ids = run_config.eval_ids
    kind = run_config.kind
    fmt = options.fmt
    changed_from = options.changed_from
    skill_b = options.skill_b
    label_a = options.label_a
    label_b = options.label_b
    from sift.execution.runner import build_sides

    lc = _log_console(fmt)
    _wall_start = (
        time.perf_counter()
    )  # 命令起点;phase1_wall = 此处到 run_ab_test 结束(含 setup),aggregate 自测 phase2,total=phase1+phase2

    # 三选一互斥:samples(随机估)/ ids(手动精确)/ changed-from(自动精确)。
    _selectors = [eval_ids, eval_samples, changed_from]
    if sum(1 for s in _selectors if s) > 1:
        raise SiftError("--eval-ids / --eval-samples / --changed-from 三选一互斥(指定其一就不再用别的选题)")

    # changed-from:路 A,resolve 成 eval_ids 复用现成 by-id 通道。RunConfig/runner 不动。
    selection = None
    if changed_from:
        selection, eval_ids = _affected_selection(skill_path, changed_from, eval_set, model, lc)

    # sides spec 单一事实源:skill_b 给定 → v1/v2 两 skill;None → 默认 with/without。
    sides = build_sides(skill_path, skill_b, label_a, label_b, kind=kind)

    total_evals, actual_evals, selected, sampled = _selection_summary(eval_set, eval_ids, eval_samples, changed_from)
    lc.print(_format_run_header(RunHeader(len(sides), eval_set.skill_name, total_evals, selected, sampled, runs)))

    scoring_config = ScoringConfig(
        enable_programmatic=scoring in ("hybrid", "programmatic"),
        enable_llm=scoring in ("hybrid", "llm"),
        llm_model=model,
        llm_timeout=timeout,
    )
    run_config = run_config.model_copy(
        update={
            "eval_ids": eval_ids,
            "per_eval_timeout": timeout,
            "min_runs_for_stats": max(3, runs),
        }
    )

    context = _EvaluationPresentation(sides, actual_evals, selection, fmt, _wall_start)
    return _execute_evaluation_pipeline(skill_path, eval_set, (run_config, scoring_config), context)


def _make_progress_callback(progress_state):
    """将执行层的具名事件原样交给进度渲染器。"""
    return progress_state.update


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
        f"[dim]缓存:{progress_state.skipped}/{progress_state.total_runs} 个槽复用既有结果未重跑(内容未变)。[/dim]",
    ]


def _print_benchmark_result(benchmark, benchmark_path: Path, fmt: str) -> None:
    """显示同一份 benchmark 的 JSON 或终端摘要。"""
    if fmt == "json":
        console.print(json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        # side key 配置无关地从 run_summary 取(with_skill/without_skill 或 v1/v2)
        _print_benchmark_terminal(benchmark, benchmark_path)


def _print_benchmark_terminal(benchmark, benchmark_path):
    summary = benchmark.run_summary
    delta = summary.get("delta", {})
    analysis = getattr(benchmark, "analysis", None)
    cs = getattr(benchmark, "comparison_summary", None)  # 主观侧主位

    side_keys = [k for k in summary.keys() if k != "delta"]
    a_mean = (summary.get(side_keys[0], {}).get("pass_rate", {}).get("mean", 0) * 100) if side_keys else 0
    b_mean = (summary.get(side_keys[1], {}).get("pass_rate", {}).get("mean", 0) * 100) if len(side_keys) > 1 else 0
    pr_delta = delta.get("pass_rate", "—")
    la = cs.label_a if cs else (side_keys[0] if side_keys else "side A")
    lb = cs.label_b if cs else (side_keys[1] if len(side_keys) > 1 else "side B")

    console.print(f"\n[bold]Results:[/bold]")

    # 1. Quality — blind comparison (主观侧主位)
    if cs and cs.valid > 0:
        console.print(f"  [green]Quality:[/green]   {la} 盲评胜率 {cs.win_rate * 100:.0f}% ({cs.a_wins}/{cs.valid})")
    else:
        console.print(f"  Quality:   (add --model for blind comparison)")

    # 2. Pass rate — mechanical assertions (客观侧;agent 评测只报绝对,两臂报 vs+delta)
    console.print(_format_pass_rate_line(side_keys, PassRateSide(la, a_mean), PassRateSide(lb, b_mean), pr_delta))

    # 3. Analysis insights
    _print_analysis_strengths(analysis)

    # 4. 失败根因汇总(失败三角化):有 error run 才打印
    for _line in format_failure_triage(benchmark.runs):
        console.print(_line)

    console.print(f"\nBenchmark saved to: {benchmark_path}")


def _print_analysis_strengths(analysis):
    if analysis and isinstance(analysis, dict):
        strengths = analysis.get("winner_strengths", [])
        if strengths:
            console.print(f"\n[bold]Key strengths:[/bold]")
            for s in strengths[:2]:
                console.print(f"  • {s}")


def _print_aggregation_error(exc: Exception, iteration_dir: Path) -> None:
    """汇总失败时显示目录布局，帮助定位结果缺失。"""
    console.print(f"[red]Error:[/red] Benchmark aggregation failed: {exc}")
    console.print(f"  iteration_dir: {iteration_dir}")
    configs = [d.name for d in iteration_dir.iterdir() if d.is_dir()]
    console.print(f"  config dirs found: {configs}")
    for c in configs:
        cd = iteration_dir / c
        runs_direct = list(cd.glob("run-*"))
        runs_nested = list(cd.glob("eval-*/run-*"))
        console.print(f"    {c}/: {len(runs_direct)} direct runs, {len(runs_nested)} nested runs")


def _selection_summary(eval_set, eval_ids, eval_samples, changed_from) -> tuple:
    """计算实际选题数和启动提示，保留百分比取整规则。"""
    total_evals = len(eval_set.evals)
    actual_evals = total_evals
    selected = ""
    if eval_ids:
        actual_evals = len(eval_ids)
        _label = "changed-from" if changed_from else "by id"
        selected = f" ({_label}: {','.join(map(str, eval_ids))} = {actual_evals}/{total_evals})"
    sampled = ""
    if eval_samples:
        if isinstance(eval_samples, float) and eval_samples <= 1.0:
            actual_evals = max(1, int(total_evals * eval_samples))
            sampled = f" (sampled {int(eval_samples * 100)}% = {actual_evals}/{total_evals})"
        else:
            actual_evals = min(int(eval_samples), total_evals)
            sampled = f" (sampled {actual_evals}/{total_evals})"
    return total_evals, actual_evals, selected, sampled


def _affected_selection(skill_path: Path, changed_from: Path, eval_set, model: str | None, lc) -> tuple:
    """分析变更影响的考题；没有受影响考题时按原约定退出。"""
    from sift.cli.affected import compute_affected_evals

    affected = compute_affected_evals(skill_path, changed_from, eval_set, model)
    total_evals_pre = len(eval_set.evals)
    selection = {
        "mode": "changed-from",
        "ids": affected,
        "affected": len(affected),
        "total": total_evals_pre,
    }
    lc.print(f"changed-from: 受影响 {affected} of {total_evals_pre} 道题 → 跑子集")
    if not affected:
        console.print(
            "[yellow]判定无受影响 eval(改的可能是与所有题无关的措辞)。不放心去掉 --changed-from 跑全量。[/yellow]"
        )
        raise typer.Exit(code=0)
    eval_ids = affected  # 灌进 by-id 通道
    return selection, eval_ids


def _execute_evaluation_pipeline(skill_path: Path, eval_set, configs: tuple, context: _EvaluationPresentation) -> tuple:
    """执行评测、聚合及结果输出，保留各阶段退出码和计时边界。"""
    from sift.execution.benchmark import BenchmarkAggregator
    from sift.execution.runner import ABTestRunner

    run_config, scoring_config = configs
    sides, actual_evals, selection, fmt, _wall_start = context
    runs, model, output_dir = run_config.runs_per_config, run_config.model, run_config.output_dir
    progress_state = _ProgressState(actual_evals, runs, num_sides=len(sides))
    on_progress = _make_progress_callback(progress_state)

    try:
        ab = ABTestRunner(run_config, scoring_config, on_progress=on_progress)
        iteration_dir = ab.run_ab_test(skill_path, eval_set, sides=sides)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    agg = BenchmarkAggregator(model=model, enable_llm=scoring_config.enable_llm)
    try:
        benchmark = agg.aggregate(iteration_dir, skill_path, sides=sides, phase1_wall=time.perf_counter() - _wall_start)
    except Exception as exc:
        _print_aggregation_error(exc, iteration_dir)
        raise typer.Exit(code=1) from exc

    if selection:
        benchmark.selection = selection

    benchmark_path = iteration_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    _print_benchmark_result(benchmark, benchmark_path, fmt)

    # 缓存复用提示:全跳过 = 输入未变、结果可信复用;部分跳过也明说。避免"进度条瞬间 100%"的困惑。
    for line in _cache_notice_lines(progress_state, output_dir):
        console.print(line)

    return benchmark, iteration_dir
