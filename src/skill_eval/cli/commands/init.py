from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.parser import resolve_skill_dir


@app.command()
def init(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory for evals.json"),
    generate_prompts: bool = typer.Option(True, "--generate-prompts/--no-generate-prompts", help="Use LLM to generate L1 unit-level evals"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for LLM generation"),
    num_evals: Optional[int] = typer.Option(None, "--num-evals", help="Max number of eval cases to generate"),
    from_transcript: Optional[Path] = typer.Option(
        None, "--from-transcript",
        help="从真实 session transcript 挖 evals(加料 SKILL.md,非替代)",
    ),
):
    """Initialize evals.json for a Skill.
    
    Generates L1 unit test cases from SKILL.md content.
    """
    skill_path = resolve_skill_dir(skill_path)
    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=model, timeout=300)

        if from_transcript:
            console.print(f"Mining evals from transcript [bold]{from_transcript}[/bold]...")
            eval_set = gen.generate_from_transcript(
                skill_path, from_transcript, on_heartbeat=lambda m: console.print(m)
            )
        elif generate_prompts:
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
        degraded = [(e, f) for e in eval_set.evals for f in (e.flags or []) if f.startswith("degraded:")]
        if degraded:
            console.print(f"[yellow]⚠️  {len(degraded)} 个 eval 为 degraded fallback(LLM 失败,未挖到结构化信号,assertions 可能为空):[/yellow]")
            for e, f in degraded:
                console.print(f"[yellow]   - {e.name}: {f}[/yellow]")
            console.print("[yellow]建议重跑,或检查 claude CLI 可用性。[/yellow]")
        for e in eval_set.evals:
            level = getattr(e, 'level', 'L1')
            console.print(f"  [{level}] {e.name}: {len(e.expectations)} expectations" +
                          (f", {len(e.fixtures)} fixtures" if e.fixtures else ""))
        console.print(f"Written to {output}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
