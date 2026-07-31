from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.evals.inspect import same_prompt_groups
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
    kind: Optional[str] = typer.Option(
        None, "--kind",
        help="声明单元种类:只挖本 skill/agent 被调用/被 dispatch 期间段(skill=按 Skill 调用切;"
             "agent=按 agent 归属切,同 audit --kind)",
    ),
    timeout: int = typer.Option(
        600, "--timeout",
        help="LLM 调用超时秒数(mining 是单次大 prompt,默认 600;撞超时 → degraded fallback)",
    ),
):
    """Initialize evals.json for a Skill.
    
    Generates L1 unit test cases from SKILL.md content.
    """
    skill_path = resolve_skill_dir(skill_path)
    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=model, timeout=timeout)

        if from_transcript:
            if kind is not None and kind not in ("skill", "agent"):
                console.print(f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {kind!r}")
                raise typer.Exit(code=2)
            if not kind:
                from skill_eval.audit.slice import invoked_skills
                skills = invoked_skills(from_transcript)
                if len(skills) > 1:
                    console.print(
                        f"[yellow]⚠️  transcript 涉及 {len(skills)} 个 skill"
                        f"({', '.join(skills)}),会把全部当本 skill 挖、可能串味;"
                        f"加 --kind skill 只挖本 skill 被调用期间段。[/yellow]"
                    )
            console.print(f"Mining evals from transcript [bold]{from_transcript}[/bold]...")
            eval_set = gen.generate_from_transcript(
                skill_path, from_transcript,
                on_heartbeat=lambda m: console.print(m), kind=kind,
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
            line = (f"  [{level}] {e.name}: {len(e.assertions)} assertions, "
                    f"{len(e.expectations)} expectations"
                    + (f", {len(e.fixtures)} fixtures" if e.fixtures else ""))
            if e.confidence is not None:
                line += f" · conf {e.confidence:.2f}"
            console.print(line)
            rat = (e.rationale or "").strip()
            if rat:
                console.print(f"      ↳ {rat[:140]}")
        for g in same_prompt_groups(eval_set):
            prompt_short = g["prompt"][:60] + ("…" if len(g["prompt"]) > 60 else "")
            flag_note = ",带 split flag" if g["split_flag"] else ""
            console.print(
                f"[cyan]ℹ[/cyan] 含同 prompt 题组:「{prompt_short}」× {g['count']} 道"
                f"(不同 scenario{flag_note},非重复)"
            )
        console.print(f"Written to {output}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
