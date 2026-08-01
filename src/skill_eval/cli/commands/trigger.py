import hashlib
import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.parser import resolve_skill_dir, parse_skill_md
from skill_eval.models import EvalSet
from skill_eval.models.trigger_report import TriggerReport, ProbeResult
from skill_eval.execution.trigger_runner import TriggerRunner
from skill_eval.execution.trigger_suggest import build_rule_suggestions


@app.command()
def trigger(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output dir"),
    model: Optional[str] = typer.Option(None, "--model"),
    runs: int = typer.Option(1, "--runs", help="每 probe 重复跑几次(置信度)"),
    timeout: int = typer.Option(120, "--timeout"),
    max_turns: int = typer.Option(10, "--max-turns"),
    evals_file: Optional[Path] = typer.Option(None, "--evals", help="evals.json 路径(默认 skill/evals/evals.json)"),
):
    """测 cc 会不会按 skill description 自然触发(第 4 维 trigger-accuracy)。"""
    skill_path = resolve_skill_dir(skill_path)
    try:
        name, description, *_ = parse_skill_md(skill_path)
        ef = evals_file or (skill_path / "evals" / "evals.json")
        eval_set = EvalSet.model_validate(json.loads(ef.read_text(encoding="utf-8")))
        probes = eval_set.trigger_probes
        if not probes:
            console.print(f"[yellow]⚠️ {ef} 无 trigger_probes(先跑 init --trigger-probes 生成)。[/yellow]")
            raise typer.Exit(code=0)
        out_dir = output or (skill_path / "evals")
        out_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"Trigger-testing [bold]{name}[/bold]({len(probes)} probes × {runs} runs)...")
        runner = TriggerRunner(skill_path, model=model, timeout=timeout, max_turns=max_turns, runs=runs)
        res = runner.run(probes, out_dir)
        report = TriggerReport(
            skill_name=name,
            description=description,
            description_hash=hashlib.sha256(description.encode()).hexdigest()[:16],
            runs=runs, recall=res["recall"], false_trigger_rate=res["false_trigger_rate"],
            n_should_trigger=res["n_should_trigger"], n_should_not=res["n_should_not"],
            unresolved=res["unresolved"],
            token_usage={"total_tokens": sum(p["tokens"] for p in res["probes"])},
            probes=[ProbeResult(id=p["id"], expect_trigger=p["expect_trigger"],
                                prompt=p["prompt"],
                                trigger_rate=p["trigger_rate"], verdict=p["verdict"],
                                transcript_path=p["transcript_path"]) for p in res["probes"]],
        )
        report.suggestions = build_rule_suggestions(res["probes"])
        (out_dir / "trigger-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
        recall_s = f"{res['recall']*100:.0f}%" if res["recall"] is not None else "N/A"
        ftr_s = f"{res['false_trigger_rate']*100:.0f}%" if res["false_trigger_rate"] is not None else "N/A"
        console.print(f"recall [green]{recall_s}[/green]({res['n_should_trigger']} should-trigger)"
                      f" · false-trigger [red]{ftr_s}[/red]({res['n_should_not']} should-not)"
                      f" · {runs} runs · {res['unresolved']} unresolved")
        for p in res["probes"]:
            rate_s = f"{p['trigger_rate']:.0%}" if p["trigger_rate"] is not None else "?"
            console.print(f"  [{'+' if p['verdict']=='PASS' else '-' if p['verdict']=='FAIL' else '?'}] "
                          f"probe-{p['id']}: {rate_s} → {p['verdict']}")
        if report.suggestions:
            console.print("[bold]操作建议:[/bold]")
            for s in report.suggestions:
                console.print(f"  [dim][{s.source}:{s.category}][/dim] {s.message}")
        console.print(f"Written to {out_dir / 'trigger-report.json'}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=getattr(exc, "exit_code", 1))
