from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.helpers import _check_claude_or_exit
from skill_eval.cli.main import app, console


@app.command(name="generate-config")
def generate_config(
    skills_dir: Path = typer.Argument(..., help="Root directory containing skill collections"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output YAML path (default: <dir>/eval-config.yaml)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model for claude -p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan only, don't call LLM"),
):
    """Generate eval-config.yaml from skills directory using LLM."""
    from skill_eval.config.generator import ConfigGenerator

    if not dry_run:
        _check_claude_or_exit()

    gen = ConfigGenerator()
    out_path = output or skills_dir / "eval-config.yaml"

    try:
        result = gen.generate(skills_dir, output_path=out_path, model=model, dry_run=dry_run)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if dry_run:
        meta = result.get("metadata", {})
        console.print(f"[bold]Dry run:[/bold] {meta.get('total_skills', 0)} skills found")
        console.print(f"  Domains: {meta.get('domains', [])}")
        console.print(f"  Types: {meta.get('types', [])}")
        console.print(f"  Prefixes: {meta.get('collection_prefixes', [])}")
        console.print(f"  Prompt length: {result.get('prompt_length', 0)} chars")
    else:
        domains = list(result.get("domain_templates", {}).keys())
        types = list(result.get("type_templates", {}).keys())
        console.print(f"Config written to: {out_path}")
        console.print(f"  Domains: {domains}")
        console.print(f"  Types: {types}")
        console.print(f"  Integration scenarios: {len(result.get('integration_scenarios', []))}")
        console.print(f"  Skill keywords: {len(result.get('skill_keywords', {}))}")
        console.print(f"  Pipelines: {len(result.get('pipelines', []))}")
