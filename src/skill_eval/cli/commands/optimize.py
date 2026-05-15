from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.helpers import _check_claude_or_exit
from skill_eval.cli.main import app, console


@app.command(name="optimize-trigger")
def optimize_trigger(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    eval_set: Path = typer.Option(..., "--eval-set", help="Path to trigger eval set JSON"),
    max_iterations: int = typer.Option(5, "--max-iterations", help="Max optimization iterations"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
):
    """Optimize Skill trigger description."""
    _check_claude_or_exit()
    from skill_eval.models import LoopConfig, TriggerQuery
    from skill_eval.optimization.loop import OptimizationLoop

    try:
        raw = json.loads(eval_set.read_text(encoding="utf-8"))
        queries = [TriggerQuery(**q) for q in raw]
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load eval set: {exc}")
        raise typer.Exit(code=2)

    config = LoopConfig(max_iterations=max_iterations, model=model)
    loop = OptimizationLoop(config)
    result = loop.run(skill_path, queries)

    if format == "json":
        console.print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        console.print(f"[bold]Optimization complete:[/bold] {result.exit_reason}")
        console.print(f"  Original: {result.original_description[:80]}...")
        console.print(f"  Best:     {result.best_description[:80]}...")
        console.print(f"  Score:    {result.best_score:.0%} ({result.best_score_label})")
        console.print(f"  Iterations: {result.iterations_run}")
