from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from skill_eval.cli.main import app, console
from skill_eval.parser import resolve_skill_dir


def _render_security_report(report, console: Console) -> None:
    table = Table(title=f"Security Scan: {report.skill_name}")
    table.add_column("Scanner", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    for s in report.scanners:
        if s.status == "PASS":
            status = "[green]PASS[/green]"
        elif s.status == "FAIL":
            status = "[red]FAIL[/red]"
        elif s.status == "WARN":
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[dim]SKIP[/dim]"
        table.add_row(s.name, status, s.message)
    console.print(table)

    if report.findings:
        console.print("\n[bold]Findings:[/bold]")
        for f in report.findings:
            color = "red" if f.severity in ("CRITICAL", "HIGH") else "yellow"
            loc = f" ({f.file_path}" + (f":{f.line}" if f.line else "") + ")" if f.file_path else ""
            console.print(f"  [{color}]{f.severity}[/{color}] [{f.scanner}] {f.description}{loc}")

    if report.verdict == "BLOCKED":
        console.print(f"\n[red bold]VERDICT: BLOCKED[/red bold] — {report.summary}")
    elif report.verdict == "REVIEW_NEEDED":
        console.print(f"\n[yellow bold]VERDICT: REVIEW NEEDED[/yellow bold] — {report.summary}")
    else:
        console.print(f"\n[green bold]VERDICT: SAFE[/green bold] — {report.summary}")


@app.command()
def vet(
    skill_path: Path = typer.Argument(..., help="Path to skill directory or GitHub URL", exists=True),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
    skip_external: bool = typer.Option(False, "--skip-external", help="Skip external scanners (aguara, skill-scanner)"),
):
    """Run security vetting on a Skill."""
    skill_path = resolve_skill_dir(skill_path)
    from skill_eval.security.vetter import SecurityVetter

    try:
        vetter = SecurityVetter(skip_external=skip_external)
        report = vetter.vet(skill_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if format == "json":
        data = report.model_dump(mode="json")
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        _render_security_report(report, console)

    vet_output = output or skill_path / "vet-report.json"
    if not vet_output.suffix == ".json":
        vet_output = vet_output / "vet-report.json"
    vet_output.parent.mkdir(parents=True, exist_ok=True)
    vet_output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if format != "json":
        console.print(f"  Report: {vet_output}")

    if report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
        raise typer.Exit(code=1)
