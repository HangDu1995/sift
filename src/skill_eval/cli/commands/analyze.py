from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.models import AnalysisReport
from skill_eval.static_analysis.analyzer import StaticAnalyzer


@app.command()
def analyze(
    skill_path: Path = typer.Argument(..., help="Path to skill directory containing SKILL.md", exists=True),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Run static analysis on a Skill and output a scorecard."""
    try:
        analyzer = StaticAnalyzer()
        report: AnalysisReport = analyzer.analyze(skill_path)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    data = report.model_dump(mode="json")

    if format == "json":
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        table = Table(title=f"Skill Scorecard: {report.skill_name}")
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Detail")
        for check in report.checks:
            status = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
            table.add_row(check.name, status, check.message)
        table.add_section()
        table.add_row(
            "[bold]Score[/bold]",
            f"[bold]{report.score}/{report.max_score}[/bold]",
            f"Category: {report.category.value}",
        )
        console.print(table)

    if not output:
        output = skill_path / "scorecard.json"

    output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  Report: {output}")

    if report.score < report.max_score:
        raise typer.Exit(code=1)
