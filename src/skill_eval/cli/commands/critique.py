"""critique 命令:诊断 SKILL.md 正文内容质量(4 维读者反馈态,纯诊断无 gate)。"""
from pathlib import Path
from typing import Optional

import typer
from rich.text import Text

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.parser import resolve_skill_dir, parse_skill_md
from skill_eval.static_analysis.critique import analyze_content_quality

_STATUS_BADGE = {"met": "[green]满足[/green]", "partial": "[yellow]部分[/yellow]", "missing": "[red]缺失[/red]"}


@app.command()
def critique(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output dir"),
    model: Optional[str] = typer.Option(None, "--model"),
    timeout: int = typer.Option(120, "--timeout"),
):
    """诊断 SKILL.md 正文内容质量(4 维读者反馈;纯诊断无 gate)。"""
    skill_path = resolve_skill_dir(skill_path)
    try:
        name, *_ = parse_skill_md(skill_path)
        with console.status(f"正在评审 {name} 的正文内容质量…", spinner="dots"):
            report = analyze_content_quality(skill_path, model=model, timeout=timeout)

        out_dir = output or (skill_path / "evals")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "content-quality-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")

        sc = report.status_counts
        tu = report.token_usage
        tok = (tu.get("input_tokens", 0) or 0) + (tu.get("output_tokens", 0) or 0)
        console.print(f"正文内容质量 [bold]{name}[/bold]"
                      f" · 满足 [green]{sc['met']}[/green]"
                      f" · 部分 [yellow]{sc['partial']}[/yellow]"
                      f" · 缺失 [red]{sc['missing']}[/red]"
                      f" · 耗时 {report.duration_seconds:.1f}s · {tok} tok")
        for d in report.dimensions:
            console.print(f"  {d.name} {_STATUS_BADGE.get(d.status, d.status)}")
            for ev in d.evidence:
                console.print(f"      ↳ 「{ev[:160]}」", markup=False)
            if d.feedback:
                console.print(Text("      ✓ ", style="green").append(d.feedback))
            if d.gap:
                console.print(Text("      ✗ ", style="red").append(d.gap))

        from skill_eval.reporting.critique_viewer import CritiqueReportGenerator
        html_path = out_dir / "content-quality-report.html"
        CritiqueReportGenerator().write_html(report, html_path)
        console.print(f"Written to {out_dir / 'content-quality-report.json'} · {html_path}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=getattr(exc, "exit_code", 1))
