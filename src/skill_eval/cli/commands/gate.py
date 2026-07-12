"""gate — post-hoc 对已有 benchmark.json 出通过/失败判决。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.gating.verdict import evaluate_gate


@app.command()
def gate(
    benchmark_path: Path = typer.Argument(..., help="Path to benchmark.json"),
    min_win_rate: Optional[float] = typer.Option(
        None, "--min-win-rate",
        help="主观 gate 阈值(候选 tie-排除胜率);不给则只走客观回归守卫"),
    min_valid: int = typer.Option(3, "--min-valid", help="主观 gate 最少决定性比较数"),
    no_programmatic_guard: bool = typer.Option(
        False, "--no-programmatic-guard", help="关掉客观回归守卫"),
    candidate: Optional[str] = typer.Option(
        None, "--candidate",
        help="候选侧名(默认 comparison_summary.label_a / 首个 side key)"),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="gate.json 输出路径(默认 benchmark.json 同目录)"),
):
    """对已有 benchmark.json 出通过/失败判决(post-hoc,毫秒级,可反复改阈值重判)。

    两轴:客观 programmatic 回归守卫(默认常开)+ 主观 comparison(--min-win-rate opt-in)。
    退出码:0=PASS / 1=FAIL / 2=N/A(信号不足)。判决写 gate.json。
    """
    # exit 2 双语义:文件/解析错误(此处,不写 gate.json)与 N/A 判决(下方写 gate.json)
    # 都退 2——CI 靠 gate.json 是否存在区分「使用错误」vs「信号不足」。
    if not benchmark_path.exists():
        console.print(f"[red]Error:[/red] not found: {benchmark_path}")
        raise typer.Exit(code=2)
    try:
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Error:[/red] failed to parse benchmark.json: {exc}")
        raise typer.Exit(code=2)

    result = evaluate_gate(
        benchmark, min_win_rate=min_win_rate, min_valid=min_valid,
        programmatic_guard=not no_programmatic_guard, candidate=candidate,
    )
    result.artifact = str(benchmark_path)

    gate_json_path = out or (benchmark_path.parent / "gate.json")
    gate_json_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    color = {"PASS": "green", "FAIL": "red", "N/A": "yellow"}[result.decision]
    console.print(f"[{color}][{result.decision}][/{color}] {result.reason}")
    for w in result.warnings:
        console.print(f"[yellow]⚠ {w}[/yellow]")
    console.print(f"gate verdict → {gate_json_path}")
    raise typer.Exit(code=result.exit_code)
