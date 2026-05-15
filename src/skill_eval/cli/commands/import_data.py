from __future__ import annotations

import json
from pathlib import Path

import typer

from skill_eval.cli.main import app, console


@app.command(name="import")
def import_data(
    jsonl_path: Path = typer.Argument(..., help="Path to skill-monitor JSONL file"),
    benchmark: Path = typer.Option(..., "--benchmark", help="Path to benchmark.json to augment"),
):
    """Import skill-monitor JSONL data into benchmark."""
    from skill_eval.integration.monitor_importer import MonitorImporter
    from skill_eval.models import BenchmarkData

    if not jsonl_path.exists():
        console.print(f"[red]Error:[/red] JSONL file not found: {jsonl_path}")
        raise typer.Exit(code=2)

    if not benchmark.exists():
        console.print(f"[red]Error:[/red] Benchmark file not found: {benchmark}")
        raise typer.Exit(code=2)

    try:
        bench_data = BenchmarkData.model_validate_json(benchmark.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to parse benchmark.json: {exc}")
        raise typer.Exit(code=2)

    importer = MonitorImporter()
    updated = importer.import_jsonl(jsonl_path, bench_data)

    benchmark.write_text(
        json.dumps(updated.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]Updated[/green] {benchmark} with monitor data from {jsonl_path}")
