"""freeze — snapshot an evals.json into a named baseline + provenance sidecar."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from sift.cli.main import app, console
from sift.evals.baseline import freeze_baseline


@app.command(rich_help_panel="出题")
def freeze(
    evals_path: Path = typer.Argument(..., help="Path to source evals.json"),
    name: str = typer.Option("baseline", "--name", help="Baseline name (-> <name>.json + <name>.meta.json)"),
    out: Optional[Path] = typer.Option(None, "--out", help="Output dir (default: beside evals.json)"),
    skill_version: Optional[str] = typer.Option(
        None, "--skill-version", help="Target skill version (recorded in meta)"
    ),
):
    """把 evals.json 冻结成命名基线快照 + 出处元数据。

    Produces <name>.json (standard evals format, loader-unchanged) + <name>.meta.json
    (provenance sidecar). Where it lives / whether committed is your deployment call
    (CI: commit it so checkout has it). Overwrites same-name (idempotent).
    """
    if not evals_path.exists():
        console.print(f"[red]Error:[/red] not found: {evals_path}")
        raise typer.Exit(code=2)
    try:
        baseline_path, meta_path = freeze_baseline(
            evals_path,
            name=name,
            out_dir=out,
            target_skill_version=skill_version,
        )
    except Exception as exc:
        console.print(f"[red]Error:[/red] failed to freeze: {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]Frozen[/green] → {baseline_path}")
    console.print(f"          + {meta_path}")
