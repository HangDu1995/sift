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
    layers: str = typer.Option("unit,integration,regression", "--layers", help="Test layers to run"),
    baseline: Optional[Path] = typer.Option(None, "--baseline", help="Baseline benchmark.json for regression"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop on first failure"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    pipeline: Optional[str] = typer.Option(None, "--pipeline", help="Run a specific pipeline by name"),
    pipeline_steps: Optional[str] = typer.Option(None, "--pipeline-steps", help="Comma-separated step names to run (with auto dep resolution)"),
    project: Optional[str] = typer.Option(None, "--project", help="Override project path for pipeline"),
    projects: Optional[str] = typer.Option(None, "--projects", help="Comma-separated project paths to override pipeline projects"),
):
    """Run batch evaluation across multiple Skills."""
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

    pipeline_steps_list = [s.strip() for s in pipeline_steps.split(",")] if pipeline_steps else None
    project_overrides = [p.strip() for p in projects.split(",")] if projects else ([project] if project else None)

    layer_list = [l.strip() for l in layers.split(",")]
    if "unit" in layer_list:
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

        cascade = scheduler.generate_cascade_report(results)

        console.print(f"\n  Passed: {cascade['passed']}, Failed: {cascade['failed']}, Skipped: {cascade['skipped']}")
        if cascade["attributions"]:
            console.print("  Attributions:")
            for skill, attr in cascade["attributions"].items():
                console.print(f"    {skill}: {attr}")

        cascade_path = output_dir / "cascade-report.json"
        cascade_path.write_text(json.dumps(cascade, indent=2, ensure_ascii=False), encoding="utf-8")

        if cascade.get("failed", 0) > 0:
            raise typer.Exit(code=1)

    if "integration" in layer_list:
        console.print(f"[bold]Running integration tests...[/bold]")
        from skill_eval.config.loader import load_config

        scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir)
        project_config = load_config(base_dir)
        int_result = scheduler.run_integration_tests(
            output_dir,
            config=project_config,
            pipeline_name=pipeline,
            pipeline_steps=pipeline_steps_list,
            project_overrides=project_overrides,
        )

        console.print(f"  Integration: {int_result['passed']}/{int_result['total']} passed")
        if int_result["scenarios"]:
            for sc in int_result["scenarios"]:
                icon = "[green]✓[/green]" if sc.get("passed") else "[red]✗[/red]"
                console.print(f"    {icon} {sc.get('name', '?')} ({sc.get('type', '?')})")

        int_path = output_dir / "integration-report.json"
        int_path.write_text(json.dumps(int_result, indent=2, ensure_ascii=False), encoding="utf-8")

        if int_result.get("failed", 0) > 0:
            raise typer.Exit(code=1)

    if "regression" in layer_list:
        console.print(f"[bold]Running regression tests...[/bold]")
        if not baseline:
            console.print("[yellow]Warning:[/yellow] No --baseline provided, skipping regression")
        else:
            scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir)
            reg_result = scheduler.run_regression_tests(output_dir, baseline)

            console.print(f"  Regression: {reg_result['passed']}/{reg_result['total']} passed")
            for entry in reg_result.get("regressions", []):
                status = entry.get("status", "?")
                if status == "REGRESSED":
                    icon, color = "✗", "red"
                elif status == "IMPROVED":
                    icon, color = "✓", "green"
                else:
                    icon, color = "○", "dim"
                delta_str = f" ({entry['delta']:+.2f})" if entry.get("delta") is not None else ""
                console.print(f"    [{color}]{icon}[/{color}] {entry['skill']}  {status}{delta_str}")

            reg_path = output_dir / "regression-delta.json"
            reg_path.write_text(json.dumps(reg_result, indent=2, ensure_ascii=False), encoding="utf-8")

            if reg_result.get("failed", 0) > 0:
                raise typer.Exit(code=1)
