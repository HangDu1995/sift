from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from skill_eval.cli.helpers import _check_claude_or_exit, _discover_evals, _parse_eval_samples
from skill_eval.cli.main import app, console
from skill_eval.cli.pipeline import _run_ab_eval
from skill_eval.errors import SkillEvalError


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
def run(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    evals: Optional[Path] = typer.Option(None, "--evals", help="Path to evals.json"),
    runs: int = typer.Option(3, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(300, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    scoring: str = typer.Option("hybrid", "--scoring", help="Scoring mode: hybrid, programmatic, or llm"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    no_calibration: bool = typer.Option(False, "--no-calibration", help="Skip scoring calibration"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    format: str = typer.Option("table", "--format", help="Output format"),
    eval_samples: Optional[str] = typer.Option(None, "--eval-samples", help="Number or pct of evals to run (e.g. 10 or 0.25)"),
    merged: bool = typer.Option(False, "--merged", help="Use merged-prompt mode instead of per-eval"),
):
    """Run A/B evaluation of a Skill."""
    from skill_eval.evals.loader import EvalLoader
    _check_claude_or_exit()

    if security_gate:
        from skill_eval.security.vetter import SecurityVetter
        vet_report = SecurityVetter(skip_external=False).vet(skill_path)
        _render_security_report(vet_report, console)
        if vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
            raise typer.Exit(code=1)

    evals_path = evals or _discover_evals(skill_path)
    if not evals_path:
        return

    try:
        loader = EvalLoader()
        eval_set = loader.load(evals_path)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    if not output_dir:
        output_dir = Path("output")

    if runs < 2:
        console.print(
            "[yellow]Warning:[/yellow] --runs 1 means no statistical validity. "
            "Single-run results have high variance and outlier detection disabled. "
            "Use --runs 3+ for reliable results."
        )

    _run_ab_eval(
        skill_path=skill_path,
        eval_set=eval_set,
        runs=runs,
        model=model,
        workers=workers,
        timeout=timeout,
        max_turns=max_turns,
        scoring=scoring,
        no_calibration=no_calibration,
        output_dir=output_dir,
        fmt=format,
        eval_samples=_parse_eval_samples(eval_samples),
        per_eval_mode=not merged,
    )
