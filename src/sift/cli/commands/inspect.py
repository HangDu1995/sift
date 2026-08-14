"""inspect — static health report for an evals.json (metrics + flags, no verdict)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from sift.cli.main import app, console
from sift.evals.inspect import inspect_evals
from sift.evals.loader import EvalLoader


@app.command()
def inspect(
    evals_path: Path = typer.Argument(..., help="Path to evals.json"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
):
    """Inspect an eval set's health: completeness, stability, problems.

    Static only (no execution, no LLM). Reports metrics + concern flags;
    does NOT verdict gate-worthiness — the human decides whether to freeze.
    """
    if not evals_path.exists():
        console.print(f"[red]Error:[/red] not found: {evals_path}")
        raise typer.Exit(code=2)
    try:
        eval_set = EvalLoader().load(evals_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] failed to load evals.json: {exc}")
        raise typer.Exit(code=2)

    result = inspect_evals(eval_set)

    if json_out:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    comp, stab, prob = result["completeness"], result["stability"], result["problems"]
    ratio = stab["programmatic_ratio"]
    ratio_str = f"{ratio * 100:.0f}%" if ratio is not None else "—"

    console.print(f"\n[bold]题集体检[/bold] · {result['skill_name']} ({evals_path.name})")
    console.print(f"  完整性: {comp['evals']} evals · {comp['assertions']} 断言 · {comp['expectations']} expectations · 层级 {dict(comp['levels'])}")
    console.print(f"  稳定性: 程序化 {stab['programmatic']} ({ratio_str}) / LLM {stab['llm']} · expectations {stab['expectations']}(全 LLM 判)")

    if eval_set.trigger_probes:
        n_should = sum(1 for p in eval_set.trigger_probes if p.expect_trigger)
        console.print(f"  trigger 探题: {len(eval_set.trigger_probes)} 道"
                      f"(should-trigger {n_should} / should-not {len(eval_set.trigger_probes) - n_should})")

    flags = []
    if prob["degraded"]:
        flags.append(f"degraded: {', '.join(prob['degraded'])}")
    if prob["zero_validation"]:
        flags.append(f"零校验(无 assertion/expectation): {', '.join(prob['zero_validation'])}")
    if prob["zero_programmatic"]:
        flags.append(f"零程序化断言(纯主观): {', '.join(prob['zero_programmatic'])}")
    if flags:
        console.print("  [yellow]⚠ 问题:[/yellow]")
        for f in flags:
            console.print(f"     - {f}")
    else:
        console.print("  [green]✓ 无问题 flag[/green]")

    groups = result.get("same_prompt_groups") or []
    if groups:
        console.print(f"  [cyan]ℹ 同 prompt 题组:[/cyan] {len(groups)} 组(不同 scenario,非重复)")
        for g in groups:
            prompt_short = g["prompt"][:60] + ("…" if len(g["prompt"]) > 60 else "")
            line = f"     · 「{prompt_short}」× {g['count']} 道 · {_fmt_ids(g['ids'])}"
            if g["split_flag"]:
                line += f"\n       带 {g['split_flag']} → fixture 冲突,生成器有意不合并"
            console.print(line)


def _fmt_ids(ids: list[int]) -> str:
    """id 列表紧凑显示:连续段用 a–b(如 [1,2,3]→"id 1–3"),否则逗号([1,3,5]→"id 1,3,5")。"""
    if not ids:
        return "id —"
    s = sorted(ids)
    if len(s) > 1 and s == list(range(s[0], s[0] + len(s))):
        return f"id {s[0]}–{s[-1]}"
    return "id " + ",".join(str(i) for i in s)
