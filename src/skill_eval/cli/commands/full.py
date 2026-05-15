from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from skill_eval.cli.helpers import _check_claude_or_exit, _discover_evals, _log_console, _parse_eval_samples
from skill_eval.cli.main import app, console
from skill_eval.cli.pipeline import _run_ab_eval
from skill_eval.errors import SkillEvalError
from skill_eval.static_analysis.analyzer import StaticAnalyzer


def _render_security_report(report, console: Console) -> None:
    table = Table(title=f"Security Scan: {report.skill_name}")
    table.add_column("Scanner", style="bold")
    table.add_column("Status")
    table.add_column("Detail")
    for s in report.scanners:
        if s.status == "PASS":
            status = "[green]PASS[/green]"
        elif s.status == "FAIL":
            status = "[red]FAIL[/red]"
        elif s.status == "WARN":
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[dim]SKIP[/dim]"
        table.add_row(s.name, status, s.message)
    console.print(table)

    if report.findings:
        console.print("\n[bold]Findings:[/bold]")
        for f in report.findings:
            color = "red" if f.severity in ("CRITICAL", "HIGH") else "yellow"
            loc = f" ({f.file_path}" + (f":{f.line}" if f.line else "") + ")" if f.file_path else ""
            console.print(f"  [{color}]{f.severity}[/{color}] [{f.scanner}] {f.description}{loc}")

    if report.verdict == "BLOCKED":
        console.print(f"\n[red bold]VERDICT: BLOCKED[/red bold] — {report.summary}")
    elif report.verdict == "REVIEW_NEEDED":
        console.print(f"\n[yellow bold]VERDICT: REVIEW NEEDED[/yellow bold] — {report.summary}")
    else:
        console.print(f"\n[green bold]VERDICT: SAFE[/green bold] — {report.summary}")


@app.command(name="full")
def full(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    runs: int = typer.Option(3, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(600, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(20, "--max-turns", help="Max agentic turns per claude -p run"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser"),
    format: str = typer.Option("table", "--format", help="Output format"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    eval_samples: Optional[str] = typer.Option(None, "--eval-samples", help="Number or pct of evals to run (e.g. 10 or 0.25)"),
    merged: bool = typer.Option(False, "--merged", help="Use merged-prompt mode instead of per-eval"),
):
    """One-click full evaluation pipeline."""
    from skill_eval.evals.loader import EvalLoader
    from skill_eval.reporting.viewer import ReportGenerator

    _check_claude_or_exit()

    if not output_dir:
        output_dir = Path("output")

    if runs < 2:
        console.print(
            "[yellow]Warning:[/yellow] --runs 1 means no statistical validity. "
            "Single-run results have high variance and outlier detection disabled."
        )

    total_steps = 6
    step = 0

    step += 1
    console.print(f"[bold]Step {step}/{total_steps}:[/bold] Security vetting...")
    vet_report_data = None
    try:
        from skill_eval.security.vetter import SecurityVetter
        vet_report = SecurityVetter(skip_external=False).vet(skill_path)
        _render_security_report(vet_report, console)
        vet_report_data = vet_report.model_dump(mode="json")
        if security_gate and vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
            raise typer.Exit(code=1)
    except SkillEvalError:
        pass

    step += 1
    console.print(f"[bold]Step {step}/{total_steps}:[/bold] Static analysis...")
    scorecard = None
    try:
        analyzer = StaticAnalyzer()
        scorecard = analyzer.analyze(skill_path)
        console.print(f"  Score: {scorecard.score}/{scorecard.max_score}")
        try:
            scorecard_data = scorecard.model_dump(mode="json")
        except Exception:
            scorecard_data = None
    except SkillEvalError as exc:
        console.print(f"  [yellow]Warning:[/yellow] {exc}")
        scorecard = None
        scorecard_data = None

    step += 1
    console.print(f"[bold]Step {step}/{total_steps}:[/bold] Loading evals...")
    evals_path = _discover_evals(skill_path)

    loader = EvalLoader()
    eval_set = loader.load(evals_path)

    step += 1
    lc = _log_console(format)
    lc.print(f"[bold]Step {step}/{total_steps}:[/bold] Running A/B test ({len(eval_set.evals)} evals × {runs} runs × 2 configs)...")

    benchmark, iteration_dir = _run_ab_eval(
        skill_path=skill_path,
        eval_set=eval_set,
        runs=runs,
        model=model,
        workers=workers,
        timeout=timeout,
        max_turns=max_turns,
        scoring="hybrid",
        no_calibration=False,
        output_dir=output_dir,
        fmt=format,
        eval_samples=_parse_eval_samples(eval_samples),
        per_eval_mode=not merged,
    )

    step += 1
    lc = _log_console(format)
    lc.print(f"[bold]Step {step}/{total_steps}:[/bold] Overfitting detection...")
    try:
        from skill_eval.overfitting.detector import OverfittingDetector
        bench_path = iteration_dir / "benchmark.json"
        if bench_path.exists():
            det = OverfittingDetector()
            bench_data_for_of = json.loads(bench_path.read_text(encoding="utf-8"))
            of_skill_name = bench_data_for_of.get("metadata", {}).get("skill_name", "")
            of_report = det.build_report(skill_name=of_skill_name)
            (iteration_dir / "overfitting_report.json").write_text(
                json.dumps(of_report.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            console.print(f"  Overfitting score: {of_report.overfitting_score:.2f}")
    except Exception as exc:
        console.print(f"  [yellow]Warning:[/yellow] Overfitting detection failed: {exc}")

    step += 1
    lc = _log_console(format)
    lc.print(f"[bold]Step {step}/{total_steps}:[/bold] Generating report...")

    gen = ReportGenerator()
    report_path = iteration_dir / "report.html"
    scorecard_data = None
    if scorecard:
        try:
            scorecard_data = scorecard.model_dump(mode="json")
            (iteration_dir / "scorecard.json").write_text(
                json.dumps(scorecard_data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            scorecard_data = None

    if vet_report_data:
        (iteration_dir / "vet-report.json").write_text(
            json.dumps(vet_report_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    bench_dict = None
    try:
        bench_dict = benchmark.model_dump(mode="json")
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to serialize benchmark data: {exc}")

    if bench_dict is not None:
        gen.write_static(iteration_dir, report_path, bench_dict, scorecard=scorecard_data)
    else:
        console.print("[yellow]Warning:[/yellow] Skipping report generation due to benchmark serialization failure")

    if format != "json":
        all_notes = benchmark.notes if isinstance(benchmark.notes, list) else []
        checkpoints_data = [n for n in all_notes if isinstance(n, dict)]
        if checkpoints_data:
            console.print(f"\n[bold]Checkpoint Breakdown:[/bold]")
            for cp in checkpoints_data:
                status = "[green]PASS[/green]" if cp.get("pass_rate", 0) >= 0.8 else "[yellow]PARTIAL[/yellow]" if cp.get("pass_rate", 0) >= 0.5 else "[red]FAIL[/red]"
                console.print(f"  {status} {cp.get('eval_name', '?')} — {cp.get('passed', 0)}/{cp.get('total', 0)} ({cp.get('pass_rate', 0)*100:.0f}%)")
                for exp in cp.get("expectations", []):
                    icon = "[green]✓[/green]" if exp.get("passed") else "[red]✗[/red]"
                    console.print(f"      {icon} {exp.get('text', '')[:80]}")
                for ast in cp.get("assertions", []):
                    icon = "[green]✓[/green]" if ast.get("passed") else "[red]✗[/red]"
                    console.print(f"      {icon} [dim](assertion)[/dim] {ast.get('text', '')[:80]}")
        console.print(f"\n  Report: {report_path}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{report_path.resolve()}")

    worst_grade = None
    for config_dir in iteration_dir.iterdir():
        if not config_dir.is_dir():
            continue
        run_dirs = list(config_dir.glob("run-*"))
        nested = list(config_dir.glob("eval-*/run-*"))
        if nested:
            run_dirs = nested
        for run_dir in run_dirs:
            cal_file = run_dir / "calibration.json"
            if cal_file.exists():
                try:
                    cal_data = json.loads(cal_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                grade = cal_data.get("grade", "HIGH")
                if grade == "UNRELIABLE":
                    worst_grade = "UNRELIABLE"
                    break
                if grade in ("LOW", "MEDIUM") and worst_grade != "UNRELIABLE":
                    worst_grade = grade
        if worst_grade == "UNRELIABLE":
            break

    if worst_grade == "UNRELIABLE":
        console.print("[yellow]Warning: calibration grade UNRELIABLE — scoring confidence is low[/yellow]")
        raise typer.Exit(code=1)
