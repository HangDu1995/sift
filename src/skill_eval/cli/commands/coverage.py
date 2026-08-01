import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.evals.coverage import analyze_coverage
from skill_eval.models import EvalSet
from skill_eval.parser import resolve_skill_dir, parse_skill_md


@app.command()
def coverage(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output dir"),
    model: Optional[str] = typer.Option(None, "--model"),
    timeout: int = typer.Option(120, "--timeout"),
    evals_file: Optional[Path] = typer.Option(None, "--evals", help="evals.json 路径(默认 skill/evals/evals.json)"),
):
    """诊断 evals.json 对 SKILL.md 内容点的覆盖(漏测=0 覆盖的点;纯诊断无 gate)。"""
    skill_path = resolve_skill_dir(skill_path)
    try:
        name, *_ = parse_skill_md(skill_path)
        ef = evals_file or (skill_path / "evals" / "evals.json")
        if not ef.exists():
            console.print(f"[yellow]⚠️ {ef} 不存在,先跑 init 生成考题。[/yellow]")
            raise typer.Exit(code=0)
        eval_set = EvalSet.model_validate(json.loads(ef.read_text(encoding="utf-8")))
        if not eval_set.evals:
            console.print(f"[yellow]⚠️ {ef} 无 eval,先跑 init 生成考题。[/yellow]")
            raise typer.Exit(code=0)

        console.print(f"Analyzing coverage for [bold]{name}[/bold]({len(eval_set.evals)} evals)...")
        report = analyze_coverage(skill_path, eval_set, model=model, timeout=timeout)

        out_dir = output or (skill_path / "evals")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "coverage-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")

        gaps = report.gaps
        tu = report.token_usage
        tok = (tu.get("input_tokens", 0) or 0) + (tu.get("output_tokens", 0) or 0)
        console.print(f"内容点 [bold]{len(report.points)}[/bold] · 漏测 [red]{len(gaps)}[/red]"
                      f" · 耗时 {report.duration_seconds:.1f}s · {tok} tok")
        if not gaps:
            console.print("[green]✓ 无漏测:SKILL.md 内容点均有 eval 覆盖。[/green]")
        else:
            console.print("[bold]漏测点(0 道题覆盖):[/bold]")
            for g in gaps:
                console.print(f"  · [red]{g.label}[/red]")
                if g.source_quote:
                    console.print(f"      ↳ SKILL.md:「{g.source_quote[:120]}」", markup=False)
            console.print("[dim]补题:逐条人工 review 后,用以下命令定向补(或加 --augment 自动补):[/dim]")
            for g in gaps:
                console.print(f'  init {skill_path} --append --focus "{g.label}"', markup=False)

        console.print(f"Written to {out_dir / 'coverage-report.json'}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=getattr(exc, "exit_code", 1))
