"""critique 命令:诊断声明内容质量(正文 4 维 + description 2 维,读者反馈态,纯诊断无 gate)。"""
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
    skill_path: Path = typer.Argument(..., help="Path to skill 目录或 agent/workflow 声明 .md"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output dir"),
    model: Optional[str] = typer.Option(None, "--model"),
    timeout: int = typer.Option(120, "--timeout"),
    runs: int = typer.Option(1, "--runs", help="多数投票跑 N 次(>1 启用,默认 1 单次)"),
    kind: str = typer.Option("skill", "--kind", help="声明对象:skill(默认,审 SKILL.md)/ agent(审 agent 定义正文)"),
):
    """诊断声明内容质量(正文 4 维 + description 2 维;纯诊断无 gate)。

    --kind agent 审 agent 定义正文(system prompt);默认 skill 审 SKILL.md 正文。
    """
    if kind not in ("skill", "agent"):
        console.print(f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {kind!r}")
        raise typer.Exit(code=2)
    skill_path = resolve_skill_dir(skill_path)
    try:
        name, *_ = parse_skill_md(skill_path)
        with console.status(f"正在评审 {name} 的内容质量" + (f"(跑 {runs} 次投票)" if runs > 1 else "") + "…",
                             spinner="dots"):
            report = analyze_content_quality(skill_path, model=model, timeout=timeout, runs=runs, subject=kind)

        # agent/workflow 声明是单个 .md 文件(resolve_skill_dir 故意不动非 SKILL.md,
        # 身份靠 frontmatter name)——输出落它所在目录的 evals/;目录则落自身 evals/。
        base_dir = skill_path.parent if skill_path.is_file() else skill_path
        out_dir = output or (base_dir / "evals")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "content-quality-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")

        sc = report.status_counts
        tu = report.token_usage
        tok = (tu.get("input_tokens", 0) or 0) + (tu.get("output_tokens", 0) or 0)
        console.print(f"内容质量 [bold]{name}[/bold]"
                      f" · 满足 [green]{sc['met']}[/green]"
                      f" · 部分 [yellow]{sc['partial']}[/yellow]"
                      f" · 缺失 [red]{sc['missing']}[/red]"
                      f" · 耗时 {report.duration_seconds:.1f}s · {tok} tok")
        for d in report.dimensions:
            head = f"  {d.name} {_STATUS_BADGE.get(d.status, d.status)}"
            if d.vote_tally:
                head += f" ({d.vote_tally})"
            console.print(head)
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
