from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from skill_eval.cli.main import console


def _log_console(fmt: str) -> Console:
    return Console(stderr=True) if fmt == "json" else console


def _parse_eval_samples(val: str | None) -> int | float | None:
    if not val:
        return None
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return None


def _check_claude_or_exit() -> None:
    from skill_eval.claude_version import check_claude_version

    ok, ver_msg = check_claude_version()
    if not ok:
        console.print(f"[red]Error:[/red] {ver_msg}")
        raise typer.Exit(code=3)
    lc = console
    lc.print(f"[dim]{ver_msg}[/dim]")


def _discover_evals(skill_path: Path) -> Path:
    for candidate in [skill_path / "evals" / "evals.json", skill_path / "evals.json"]:
        if candidate.exists():
            return candidate
    console.print("[red]Error:[/red] No evals.json found. Use --evals to specify path.")
    raise typer.Exit(code=2)
