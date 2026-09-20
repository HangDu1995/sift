from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from sift.cli.main import app, console
from sift.errors import SiftError
from sift.models import AnalysisReport
from sift.parser import resolve_skill_dir
from sift.static_analysis.analyzer import StaticAnalyzer


@app.command(rich_help_panel="静态体检(不跑 cc)")
def analyze(
    skill_path: Path = typer.Argument(..., help="Path to skill directory containing SKILL.md", exists=True),
    output_format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """11 项静态体检,产 scorecard(skill)。"""
    skill_path = resolve_skill_dir(skill_path)
    try:
        analyzer = StaticAnalyzer()
        report: AnalysisReport = analyzer.analyze(skill_path)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    data = report.model_dump(mode="json")

    if output_format == "json":
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
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

    # 落盘:仅当显式 -o(默认不写文件,避免污染 skill 目录)
    if output and output_format != "json":
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"Written to {output}")

    if report.score < report.max_score:
        raise typer.Exit(code=1)
