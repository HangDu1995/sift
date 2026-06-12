from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.live import Live

from skill_eval.cli.helpers import _check_claude_or_exit
from skill_eval.cli.main import app, console
from skill_eval.cli.progress import _BatchProgressState
from skill_eval.models import RunConfig, ScoringConfig


@app.command()
def batch(
    manifest_or_dir: Path = typer.Argument(..., help="Path to manifest JSON or skill directory"),
    runs: int = typer.Option(3, "--runs", help="Runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop on first failure"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
):
    """Run batch evaluation across multiple Skills (unit tests only)."""
    _check_claude_or_exit()
    from skill_eval.batch.manifest import ManifestLoader
    from skill_eval.batch.scheduler import BatchScheduler

    loader = ManifestLoader()
    if manifest_or_dir.is_dir():
        manifest = loader.discover(manifest_or_dir)
        base_dir = manifest_or_dir
    else:
        manifest = loader.load(manifest_or_dir)
        base_dir = manifest_or_dir.parent

    if not output_dir:
        output_dir = Path(f"batch-workspace")

    output_dir.mkdir(parents=True, exist_ok=True)

    run_config = RunConfig(runs_per_config=runs, model=model, max_workers=workers, max_turns=max_turns, output_dir=output_dir)
    scoring_config = ScoringConfig(enable_programmatic=True, enable_llm=True, llm_model=model)

    console.print(f"[bold]Batch evaluation:[/bold] {manifest.name} ({len(manifest.skills)} skills)")

    batch_progress = _BatchProgressState(len(manifest.skills))

    def on_batch_progress(skill_name, skill_idx, total_skills,
                          eval_name, config_name, eval_idx, total_evals,
                          completed, total, grading, run_duration=0.0):
        batch_progress.update(
            skill_name, skill_idx, total_skills,
            eval_name, config_name, eval_idx, total_evals,
            completed, total, grading, run_duration,
        )

    scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir, on_progress=on_batch_progress)

    with Live(batch_progress, console=console, refresh_per_second=1) as live:
        batch_progress.live = live
        results = scheduler.run_unit_tests(output_dir, fail_fast)

        for skill_name, status in results.items():
            batch_progress.set_skill_result(skill_name, status.value)

    passed = sum(1 for s in results.values() if s.value == "PASSED")
    failed = sum(1 for s in results.values() if s.value == "FAILED")
    skipped = sum(1 for s in results.values() if s.value == "SKIPPED")

    console.print(f"\n  Passed: {passed}, Failed: {failed}, Skipped: {skipped}")
    for skill_name, status in results.items():
        icon = {"PASSED": "[green]✓[/green]", "FAILED": "[red]✗[/red]", "SKIPPED": "[dim]○[/dim]"}.get(status.value, "?")
        console.print(f"    {icon} {skill_name}  {status.value}")

    if failed > 0:
        raise typer.Exit(code=1)
