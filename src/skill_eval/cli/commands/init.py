from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError


@app.command()
def init(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory for evals.json"),
    generate_prompts: bool = typer.Option(True, "--generate-prompts/--no-generate-prompts", help="Use LLM to generate L1 unit-level evals"),
    source_dir: Optional[Path] = typer.Option(None, "--source-dir", help="Path to source project directory for diverse eval generation"),
    domains: Optional[str] = typer.Option(None, "--domains", help="Comma-separated target domains for diverse eval generation"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for LLM generation"),
    num_evals: Optional[int] = typer.Option(None, "--num-evals", help="Max number of eval cases to generate"),
):
    """Initialize evals.json for a Skill.

    Without --source-dir: generates L1 unit test cases from SKILL.md content.
    With --source-dir: generates diverse eval cases from domains distinct from the source project,
    suitable for overfitting detection.
    """
    if source_dir:
        if not source_dir.is_dir():
            console.print(f"[red]Error:[/red] source-dir not found: {source_dir}")
            raise typer.Exit(code=2)

        from skill_eval.overfitting.source_analyzer import SourceAnalyzer
        from skill_eval.evals.diverse_generator import DiverseEvalGenerator

        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            console.print(f"[red]Error:[/red] SKILL.md not found in {skill_path}")
            raise typer.Exit(code=2)

        console.print(f"Scanning source project: {source_dir} ...")
        analyzer = SourceAnalyzer()
        skill_hints = [skill_path.name] if skill_path.is_dir() else [skill_path.stem]
        source_profile = analyzer.analyze(source_dir, skill_hints=skill_hints)
        console.print(f"  {source_profile.project_summary}")

        domain_list = [d.strip() for d in domains.split(",") if d.strip()] if domains else None
        if domain_list:
            console.print(f"Target domains: {', '.join(domain_list)}")

        console.print("Generating diverse eval cases with LLM ...")
        gen = DiverseEvalGenerator(model=model)
        eval_set = gen.generate(skill_path, source_profile, domains=domain_list)

        if output is None:
            evals_dir = skill_path / "evals"
            evals_dir.mkdir(exist_ok=True)
            output = evals_dir / "evals-general.json"
        elif output.is_dir():
            output = output / "evals-general.json"

        data = eval_set.model_dump(mode="json")
        output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        console.print(f"Generated [green]{len(eval_set.evals)}[/green] diverse eval cases for [bold]{eval_set.skill_name}[/bold]")
        for e in eval_set.evals:
            level = getattr(e, 'level', 'L1')
            extras = []
            if e.fixtures:
                extras.append(f"{len(e.fixtures)} fixtures")
            console.print(f"  [{level}] {e.name}: {len(e.expectations)} expectations" +
                          (f", {', '.join(extras)}" if extras else ""))
        console.print(f"Written to {output}")
        return

    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=None, timeout=300)

        if generate_prompts:
            console.print("Generating L1 unit-level evals with LLM...")
            eval_set = gen.generate_with_llm(skill_path)
        else:
            console.print("Generating template evals...")
            eval_set = gen.generate_template(skill_path)

        if num_evals and num_evals < len(eval_set.evals):
            console.print(f"  Trimming to {num_evals} evals (from {len(eval_set.evals)})")
            eval_set.evals = eval_set.evals[:num_evals]

        if output is None:
            evals_dir = skill_path / "evals"
            evals_dir.mkdir(exist_ok=True)
            output = evals_dir / "evals.json"
        elif output.is_dir():
            output = output / "evals.json"

        data = eval_set.model_dump(mode="json")
        output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        console.print(f"Generated [green]{len(eval_set.evals)}[/green] eval cases for [bold]{eval_set.skill_name}[/bold]")
        for e in eval_set.evals:
            level = getattr(e, 'level', 'L1')
            console.print(f"  [{level}] {e.name}: {len(e.expectations)} expectations" +
                          (f", {len(e.fixtures)} fixtures" if e.fixtures else ""))
        console.print(f"Written to {output}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
