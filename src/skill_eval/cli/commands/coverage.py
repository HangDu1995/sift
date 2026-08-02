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
    augment: bool = typer.Option(
        False, "--augment",
        help="自动补漏测题:每个漏测点调 init 生成逻辑补题(带 review:coverage-augment flag,按 prompt 去重),写回 evals.json。",
    ),
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

        # 单次阻塞 LLM 调用(call_claude 非流式),用 spinner 给「分析中」反馈;非交互终端自动降级。
        with console.status(
            f"正在分析 {name} 的内容点 × eval 覆盖({len(eval_set.evals)} evals)…",
            spinner="dots",
        ):
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
                safe_label = g.label.replace('"', "'")
                console.print(f'  init {skill_path} --append --focus "{safe_label}"', markup=False)

        if augment and gaps:
            from skill_eval.evals.generator import EvalInitGenerator
            existing_prompts = {e.prompt for e in eval_set.evals}
            next_id = max((e.id for e in eval_set.evals), default=0) + 1
            added, skipped = 0, 0
            n_gaps = len(gaps)
            for idx, g in enumerate(gaps, 1):
                console.print(f"[bold]补题 {idx}/{n_gaps}[/bold] · {g.label}")
                gen = EvalInitGenerator(model=model, timeout=timeout, focus=g.label)
                try:
                    with console.status(f"正在为「{g.label}」生成补题…", spinner="dots"):
                        # 喂现有 evals(含本批已补题)→ 共享内核定向补题 + 语义去重;
                        # 下面的 prompt 字面去重作兜底。
                        new_set = gen.generate_with_llm(skill_path, existing_evals=eval_set.evals)
                except RuntimeError:
                    console.print(f"[dim]补「{g.label}」失败(限流/异常),跳过。[/dim]")
                    continue
                for e in new_set.evals:
                    if e.prompt in existing_prompts:
                        skipped += 1
                        continue
                    e.id = next_id
                    next_id += 1
                    e.flags = list(e.flags or []) + ["review:coverage-augment"]
                    eval_set.evals.append(e)
                    existing_prompts.add(e.prompt)
                    added += 1
            if added:
                ef.write_text(eval_set.model_dump_json(indent=2), encoding="utf-8")
            console.print(f"[green]--augment:补 {added} 题[/green](带 review:coverage-augment,需人审)"
                          f"{f' · 跳过 {skipped} 题(与现有 prompt 重复)' if skipped else ''}")
            console.print("[dim]补题已写回 evals.json;请人审 review:coverage-augment 题后接受。[/dim]")

        from skill_eval.reporting.coverage_viewer import CoverageReportGenerator
        html_path = out_dir / "coverage-report.html"
        CoverageReportGenerator().write_html(report, html_path)
        console.print(f"Written to {out_dir / 'coverage-report.json'} · {html_path}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=getattr(exc, "exit_code", 1))
