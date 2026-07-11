from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from skill_eval.cli.helpers import _check_claude_or_exit, _discover_evals, _parse_eval_samples
from skill_eval.cli.main import app, console
from skill_eval.cli.pipeline import _run_ab_eval
from skill_eval.env_check import check_skill_env, check_eval_env
from skill_eval.errors import SkillEvalError


def _render_security_report(report, console: Console) -> None:
    table = Table(title=f"Security Scan: {report.skill_name}")
    table.add_column("Scanner", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    for s in report.scanners:
        status = {"PASS": "[green]PASS[/green]", "FAIL": "[red]FAIL[/red]", "WARN": "[yellow]WARN[/yellow]"}.get(s.status, "[dim]SKIP[/dim]")
        table.add_row(s.name, status, s.message)
    console.print(table)

    if report.findings:
        console.print("\n[bold]Findings:[/bold]")
        for f in report.findings:
            color = "red" if f.severity in ("CRITICAL", "HIGH") else "yellow"
            loc = f" ({f.file_path}" + (f":{f.line}" if f.line else "") + ")" if f.file_path else ""
            console.print(f"  [{color}]{f.severity}[/{color}] [{f.scanner}] {f.description}{loc}")

    verdict_color = {"BLOCKED": "red bold", "REVIEW_NEEDED": "yellow bold"}.get(report.verdict, "green bold")
    console.print(f"\n[{verdict_color}]VERDICT: {report.verdict}[/{verdict_color}] — {report.summary}")


@app.command()
def run(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    evals: Optional[Path] = typer.Option(None, "--evals", help="Path to evals.json"),
    task: Optional[str] = typer.Option(
        None, "--task",
        help="路径1 真任务模式:直接 spawn 跑此真实任务(不读 evals.json)。给 --task 即 live 真任务模式,产物 run-*/transcript.jsonl 可喂 audit",
    ),
    cwd: Optional[Path] = typer.Option(
        None, "--cwd",
        help="路径1 真任务模式工作目录(任务材料真实文件);拷贝隔离,cc 不碰原目录",
    ),
    runs: int = typer.Option(1, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(300, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    max_retries: int = typer.Option(2, "--max-retries", help="Max retries per eval on UNEVAL/error (each retry re-runs claude + scoring + compliance LLM; heavy skills may want 0-1 to save tokens)"),
    scoring: str = typer.Option("hybrid", "--scoring", help="Scoring mode: hybrid, programmatic, or llm"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    no_compliance: Optional[bool] = typer.Option(None, "--no-compliance/--compliance", help="Skip compliance checking (auto: on for --runs<3)"),
    no_calibration: bool = typer.Option(False, "--no-calibration", help="Skip scoring calibration"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    format: str = typer.Option("table", "--format", help="Output format"),
    eval_samples: Optional[str] = typer.Option(None, "--eval-samples", help="Number or pct of evals to run (e.g. 10 or 0.25)"),
):
    """Run A/B evaluation of a Skill, or path-1 live task (--task)."""
    from skill_eval.evals.loader import EvalLoader
    _check_claude_or_exit()

    if security_gate:
        from skill_eval.security.vetter import SecurityVetter
        vet_report = SecurityVetter(skip_external=False).vet(skill_path)
        _render_security_report(vet_report, console)
        if vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
            raise typer.Exit(code=1)

    # 路径1 真任务模式:--task 直接 spawn 真实任务(--cwd 拷贝隔离),不走 evals.json
    if task:
        from skill_eval.execution.live_runner import run_live_task
        if not output_dir:
            output_dir = Path("output")
        run_live_task(
            skill_path=skill_path,
            task=task,
            cwd=cwd,
            runs=runs,
            model=model,
            timeout=timeout,
            max_turns=max_turns,
            output_dir=output_dir,
        )
        transcripts = sorted(output_dir.glob("run-*/transcript.jsonl"))
        console.print(f"\n[bold]Live task:[/bold] {runs} run(s) → {len(transcripts)} transcript(s)")
        for t in transcripts:
            console.print(f"  [dim]{t}[/dim]")
        if transcripts:
            args = " ".join(f"--transcript {t}" for t in transcripts)
            console.print(
                f"\n[dim]Audit skill 遵循性:[/dim]\n  skill-eval audit {skill_path} {args} -o /tmp/live-audit"
            )
        return

    evals_path = evals or _discover_evals(skill_path)
    if not evals_path:
        return

    try:
        loader = EvalLoader()
        eval_set = loader.load(evals_path)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    if not output_dir:
        output_dir = Path("output")

    if runs < 3:
        console.print(
            "[dim]Tip: use --runs 3 for statistical validity (outlier detection, variance analysis).[/dim]"
        )

    # Environment check
    env_result = check_skill_env(skill_path)
    prompts = [e.prompt for e in eval_set.evals]
    env_result.update(check_eval_env(prompts))
    missing = [tool for tool, ok in env_result.items() if not ok]
    if missing:
        console.print(
            f"[yellow]Environment:[/yellow] tools referenced but not found: {', '.join(missing)}"
        )

    # Compliance: on by default for runs>=3, off for runs<3, explicit flag overrides
    if no_compliance is None:
        skip_compliance = runs < 3
    else:
        skip_compliance = no_compliance

    _run_ab_eval(
        skill_path=skill_path,
        eval_set=eval_set,
        runs=runs,
        model=model,
        workers=workers,
        timeout=timeout,
        max_turns=max_turns,
        scoring=scoring,
        no_calibration=no_calibration,
        skip_compliance=skip_compliance,
        output_dir=output_dir,
        fmt=format,
        eval_samples=_parse_eval_samples(eval_samples),
        max_retries=max_retries,
    )
