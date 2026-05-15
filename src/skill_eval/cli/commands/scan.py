from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from skill_eval.cli.main import app, console


@app.command(name="scan")
def scan(
    skills_dir: Path = typer.Argument(..., help="Root directory containing skill collections"),
    domain: Optional[str] = typer.Option(None, "--domain", help="Filter by domain"),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Filter by type"),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Scan skills for eval coverage (L0)."""
    from skill_eval.scanning.scanner import scan_evals

    filters = {}
    if domain:
        filters["domain"] = domain
    if type_filter:
        filters["type"] = type_filter

    result = scan_evals(skills_dir, filters=filters)

    if format == "json":
        text = json.dumps(result, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        table = Table(title=f"Skill Eval Coverage: {skills_dir}")
        table.add_column("Skill", style="bold")
        table.add_column("Domain")
        table.add_column("Type")
        table.add_column("Evals")
        table.add_column("Status")
        for s in result["skills"]:
            status = "[green]✓[/green]" if s["has_evals"] else "[red]✗[/red]"
            table.add_row(
                s["name"][:40],
                s.get("domain") or "—",
                s.get("type") or "—",
                str(s["eval_count"]),
                status,
            )
        console.print(table)
        console.print(f"\nTotal: {result['total_skills']}  With evals: {result['with_evals']}  Missing: {result['without_evals']}")
