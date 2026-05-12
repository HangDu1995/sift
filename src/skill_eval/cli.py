"""skill-eval CLI entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import time

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from skill_eval import __version__
from skill_eval.errors import InputError, SkillEvalError
from skill_eval.models import AnalysisReport
from skill_eval.static_analysis.analyzer import StaticAnalyzer

app = typer.Typer(
    name="skill-eval",
    help="Automated evaluation tool for Claude Code Skills.",
    no_args_is_help=True,
)
console = Console()


def _log_console(fmt: str) -> Console:
    return Console(stderr=True) if fmt == "json" else console


def _run_ab_eval(
    skill_path: Path,
    eval_set,
    runs: int,
    model: str | None,
    workers: int,
    timeout: int,
    max_turns: int,
    scoring: str,
    no_calibration: bool,
    output_dir: Path,
    fmt: str = "table",
) -> tuple:
    from skill_eval.execution.runner import ABTestRunner
    from skill_eval.execution.benchmark import BenchmarkAggregator
    from skill_eval.models import RunConfig, ScoringConfig

    lc = _log_console(fmt)
    lc.print(f"Running A/B test: {eval_set.skill_name} ({len(eval_set.evals)} evals × {runs} runs)")

    scoring_config = ScoringConfig(
        enable_programmatic=scoring in ("hybrid", "programmatic"),
        enable_llm=scoring in ("hybrid", "llm"),
        llm_model=model,
        llm_timeout=timeout,
    )
    run_config = RunConfig(
        runs_per_config=runs,
        model=model,
        timeout_seconds=timeout,
        max_workers=workers,
        max_turns=max_turns,
        output_dir=output_dir,
        skip_calibration=no_calibration,
    )

    quiet_console = Console(stderr=True) if fmt == "json" else console
    progress_state = _ProgressState(len(eval_set.evals), runs)

    def on_progress(eval_name, config_name, eval_idx, total_evals, completed, total, grading, run_duration=0.0, run_output=None, phase="", substep=""):
        progress_state.update(eval_name, config_name, eval_idx, completed, total, grading, run_duration, run_output, phase=phase, substep=substep)

    try:
        with Live(progress_state, console=quiet_console, refresh_per_second=1) as live:
            progress_state.live = live
            ab = ABTestRunner(run_config, scoring_config, on_progress=on_progress)
            iteration_dir = ab.run_ab_test(skill_path, eval_set)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    agg = BenchmarkAggregator(model=model)
    benchmark = agg.aggregate(iteration_dir, skill_path)

    benchmark_path = iteration_dir / "benchmark.json"
    benchmark_path.write_text(
        json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary = benchmark.run_summary
    delta = summary.get("delta", {})

    if fmt == "json":
        console.print(json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        ws = summary.get("with_skill", {}).get("pass_rate", {})
        wos = summary.get("without_skill", {}).get("pass_rate", {})
        console.print(f"\n[bold]Results:[/bold]")
        console.print(f"  with_skill:    {ws.get('mean', 0)*100:.0f}% pass rate")
        console.print(f"  without_skill: {wos.get('mean', 0)*100:.0f}% pass rate")
        console.print(f"  delta:         {delta.get('pass_rate', '—')}")
        console.print(f"\nBenchmark saved to: {benchmark_path}")

    pr = delta.get("pass_rate", "+0.00")
    if isinstance(pr, str) and pr.startswith("-"):
        raise typer.Exit(code=1)
    elif isinstance(pr, (int, float)) and pr < 0:
        raise typer.Exit(code=1)

    return benchmark, iteration_dir


def _check_claude_or_exit() -> None:
    from skill_eval.claude_version import check_claude_version

    ok, ver_msg = check_claude_version()
    if not ok:
        console.print(f"[red]Error:[/red] {ver_msg}")
        raise typer.Exit(code=3)
    lc = console
    lc.print(f"[dim]{ver_msg}[/dim]")


def _discover_evals(skill_path: Path) -> Path:
    for candidate in [skill_path / "evals" / "evals.json", skill_path / "evals.json"]:
        if candidate.exists():
            return candidate
    console.print("[red]Error:[/red] No evals.json found. Use --evals to specify path.")
    raise typer.Exit(code=2)


class _ProgressState:
    def __init__(self, num_evals: int, runs_per_config: int):
        self.num_evals = num_evals
        self.runs_per_config = runs_per_config
        self.total_runs = 2 * runs_per_config
        self.completed = 0
        self.current_eval = ""
        self.current_config = ""
        self.eval_idx = 0
        self.last_grading = None
        self.results: list[dict] = []
        self.live = None
        self.run_start_time: float | None = None
        self.last_run_duration: float = 0.0
        self.total_elapsed: float = 0.0
        self.phase: str = ""
        self.substep: str = ""

    def update(self, eval_name, config_name, eval_idx, completed, total, grading, run_duration=0.0,
                run_output=None, phase="", substep=""):
        self.current_eval = eval_name
        self.current_config = config_name
        self.eval_idx = eval_idx
        self.phase = phase
        self.substep = substep
        if completed > self.completed:
            self.completed = completed
        if grading is not None:
            self.last_grading = grading
            failed_expectations = []
            for e in grading.expectations:
                if not e.passed:
                    failed_expectations.append(e.text[:60])
            entry = {
                "eval": eval_name,
                "config": config_name,
                "pass_rate": grading.summary.pass_rate,
                "passed": grading.summary.passed,
                "total": grading.summary.total,
                "duration": run_duration,
                "failed_expectations": failed_expectations,
            }
            if run_output is not None:
                entry["tool_calls"] = run_output.tool_calls
                entry["num_turns"] = run_output.num_turns
                entry["stop_reason"] = run_output.stop_reason
                entry["result_text"] = run_output.result_text[:200]
            self.results.append(entry)
            self.total_elapsed += run_duration
            self.run_start_time = None
        else:
            self.run_start_time = time.time()
            self.last_run_duration = 0.0
        if self.live:
            self.live.update(self, refresh=True)

    def render(self):
        pct = self.completed / self.total_runs * 100 if self.total_runs else 0
        bar_len = 30
        filled = int(bar_len * self.completed / self.total_runs) if self.total_runs else 0
        bar = "█" * filled + "░" * (bar_len - filled)

        lines = []
        lines.append(f"[bold cyan]{'━' * 60}[/bold cyan]")
        lines.append(
            f"  [bold]Progress:[/bold] [{bar}] {pct:.0f}%  "
            f"({self.completed}/{self.total_runs} runs)"
        )

        if self.current_eval:
            tag = "[green]with_skill[/green]" if self.current_config == "with_skill" else "[yellow]without[/yellow]"

            if self.run_start_time is not None:
                elapsed = time.time() - self.run_start_time
                phase_str = f" [dim]\\[{self.phase}][/dim]" if self.phase else ""
                substep_str = f" [dim]{self.substep}[/dim]" if self.substep else ""
                status = f"[bold yellow]⏳ {elapsed:.0f}s[/bold yellow]{phase_str}{substep_str}"
            else:
                status = "[dim]starting...[/dim]"

            lines.append(
                f"  [dim]Current:[/dim] [{self.eval_idx}/{self.num_evals}] "
                f"{self.current_eval[:35]}  {tag}  {status}"
            )

        if self.results:
            avg = self.total_elapsed / len(self.results)
            remaining = self.total_runs - self.completed
            eta_s = remaining * avg
            eta_m = int(eta_s) // 60
            eta_sec = int(eta_s) % 60
            lines.append(
                f"  [dim]Avg {avg:.1f}s/run · ETA {eta_m}m{eta_sec:02d}s "
                f"({remaining} left)[/dim]"
            )
            lines.append(f"  [bold]Last run detail:[/bold]")
            r = self.results[-1]
            icon = "✓" if r["pass_rate"] >= 0.5 else "✗"
            color = "green" if r["pass_rate"] >= 0.5 else "red"
            cfg = "skill" if r["config"] == "with_skill" else "base"
            dur = f"{r.get('duration', 0):.0f}s"
            lines.append(
                f"    [{color}]{icon}[/{color}] {r['eval'][:30]:30s} {cfg} "
                f"{r['pass_rate']:.0%} ({r['passed']}/{r['total']}) [{dur}]"
            )
            if r.get("tool_calls"):
                tools_str = " ".join(f"{k}×{v}" for k, v in r["tool_calls"].items())
                lines.append(f"    [dim]tools: {tools_str}[/dim]")
            if r.get("num_turns"):
                lines.append(f"    [dim]turns: {r['num_turns']}  stop: {r.get('stop_reason', '?')}[/dim]")
            if r.get("result_text"):
                lines.append(f"    [dim]output: {r['result_text'][:80]}[/dim]")
            if r.get("failed_expectations"):
                lines.append(f"    [dim]failed expectations:[/dim]")
                for fe in r["failed_expectations"][:3]:
                    lines.append(f"      [red]✗[/red] {fe}")

            if len(self.results) > 1:
                lines.append(f"  [bold]Earlier ({min(len(self.results)-1, 4)}):[/bold]")
                for r in self.results[-5:-1]:
                    icon = "✓" if r["pass_rate"] >= 0.5 else "✗"
                    color = "green" if r["pass_rate"] >= 0.5 else "red"
                    cfg = "skill" if r["config"] == "with_skill" else "base"
                    dur = f"{r.get('duration', 0):.0f}s"
                    tc = " ".join(f"{k}×{v}" for k, v in r.get("tool_calls", {}).items())
                    line = (
                        f"    [{color}]{icon}[/{color}] {r['eval'][:25]:25s} {cfg} "
                        f"{r['pass_rate']:.0%} ({r['passed']}/{r['total']}) [{dur}]"
                    )
                    if tc:
                        line += f"  [dim]{tc}[/dim]"
                    lines.append(line)

        lines.append(f"[bold cyan]{'━' * 60}[/bold cyan]")
        return Panel("\n".join(lines), title="A/B Test", border_style="blue")

    def __rich__(self):
        return self.render()


class _BatchProgressState:
    def __init__(self, total_skills: int):
        self.total_skills = total_skills
        self.current_skill = ""
        self.skill_idx = 0
        self.current_eval = ""
        self.current_config = ""
        self.eval_idx = 0
        self.completed_runs = 0
        self.total_runs = 0
        self.skill_results: dict[str, str] = {}
        self.recent: list[dict] = []
        self.live = None
        self.run_start_time: float | None = None

    def update(self, skill_name, skill_idx, total_skills,
               eval_name, config_name, eval_idx, total_evals,
               completed, total, grading, run_duration=0.0):
        self.current_skill = skill_name
        self.skill_idx = skill_idx
        self.current_eval = eval_name
        self.current_config = config_name
        self.eval_idx = eval_idx
        self.completed_runs = completed
        self.total_runs = total
        if grading is not None:
            self.recent.append({
                "skill": skill_name,
                "eval": eval_name,
                "config": config_name,
                "pass_rate": grading.summary.pass_rate,
                "passed": grading.summary.passed,
                "total": grading.summary.total,
                "duration": run_duration,
            })
            self.run_start_time = None
        else:
            self.run_start_time = time.time()
        if self.live:
            self.live.update(self, refresh=True)

    def set_skill_result(self, skill_name: str, status: str):
        self.skill_results[skill_name] = status
        if self.live:
            self.live.update(self, refresh=True)

    def render(self):
        lines = []

        lines.append(f"[bold cyan]{'━' * 55}[/bold cyan]")
        lines.append(
            f"  [bold]Skill:[/bold] [{self.skill_idx}/{self.total_skills}] "
            f"{self.current_skill[:45]}"
        )

        tag = "[green]with_skill[/green]" if self.current_config == "with_skill" else "[yellow]without[/yellow]"

        if self.run_start_time is not None:
            elapsed = time.time() - self.run_start_time
            status = f"[bold yellow]⏳ {elapsed:.0f}s[/bold yellow]"
        else:
            status = "[dim]starting...[/dim]"

        lines.append(
            f"  [dim]Eval:[/dim]   [{self.eval_idx}] {self.current_eval[:35]}  {tag}  {status}"
        )
        lines.append("")

        done = len(self.skill_results)
        for name, status_val in self.skill_results.items():
            if status_val == "PASSED":
                icon, color = "✓", "green"
            elif status_val == "FAILED":
                icon, color = "✗", "red"
            else:
                icon, color = "○", "dim"
            lines.append(f"  [{color}]{icon}[/{color}] {name[:50]:50s}  [{color}]{status_val}[/{color}]")

        if done < self.total_skills:
            lines.append(f"  [dim]{'·' * 3} {self.total_skills - done} skill(s) remaining[/dim]")

        if self.recent:
            lines.append(f"  [dim]Latest runs (last 4):[/dim]")
            for r in self.recent[-4:]:
                icon = "✓" if r["pass_rate"] >= 0.5 else "✗"
                color = "green" if r["pass_rate"] >= 0.5 else "red"
                cfg = "skill" if r["config"] == "with_skill" else "base"
                dur = f"{r.get('duration', 0):.0f}s"
                lines.append(
                    f"    [{color}]{icon}[/{color}] {r['eval'][:25]:25s} {cfg} "
                    f"{r['pass_rate']:.0%} ({r['passed']}/{r['total']}) [{dur}]"
                )

        lines.append(f"[bold cyan]{'━' * 55}[/bold cyan]")
        return Panel("\n".join(lines), title="Batch A/B Test", border_style="magenta")

    def __rich__(self):
        return self.render()


def _version(value: bool):
    if value:
        console.print(f"skill-eval {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", callback=_version, is_eager=True, help="Show version."
    ),
):
    pass


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


@app.command()
def vet(
    skill_path: Path = typer.Argument(..., help="Path to skill directory or GitHub URL", exists=True),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
    skip_external: bool = typer.Option(False, "--skip-external", help="Skip external scanners (aguara, skill-scanner)"),
):
    """Run security vetting on a Skill."""
    from skill_eval.security.vetter import SecurityVetter

    try:
        vetter = SecurityVetter(skip_external=skip_external)
        report = vetter.vet(skill_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if format == "json":
        data = report.model_dump(mode="json")
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        _render_security_report(report, console)

    if report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
        raise typer.Exit(code=1)


@app.command()
def analyze(
    skill_path: Path = typer.Argument(..., help="Path to skill directory containing SKILL.md", exists=True),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Run static analysis on a Skill and output a scorecard."""
    try:
        analyzer = StaticAnalyzer()
        report: AnalysisReport = analyzer.analyze(skill_path)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    if format == "json":
        data = report.model_dump(mode="json")
        text = json.dumps(data, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        table = Table(title=f"Skill Scorecard: {report.skill_name}")
        table.add_column("Check", style="bold")
        table.add_column("Status")
        table.add_column("Detail")
        for check in report.checks:
            status = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
            table.add_row(check.name, status, check.message)
        table.add_section()
        table.add_row(
            "[bold]Score[/bold]",
            f"[bold]{report.score}/{report.max_score}[/bold]",
            f"Category: {report.category.value}",
        )
        console.print(table)

        if output:
            data = report.model_dump(mode="json")
            output.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    if report.score < report.max_score:
        raise typer.Exit(code=1)


@app.command()
def init(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory for evals.json"),
    generate_prompts: bool = typer.Option(True, "--generate-prompts/--no-generate-prompts", help="Use LLM to generate L1 unit-level evals"),
):
    """Initialize evals.json for a Skill."""
    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=None)

        if generate_prompts:
            console.print("Generating L1 unit-level evals with LLM...")
            eval_set = gen.generate_with_llm(skill_path)
        else:
            console.print("Generating template evals...")
            eval_set = gen.generate_template(skill_path)

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
        for e in eval_set.evals:
            level = getattr(e, 'level', 'L1')
            console.print(f"  [{level}] {e.name}: {len(e.expectations)} expectations" +
                          (f", {len(e.fixtures)} fixtures" if e.fixtures else ""))
        console.print(f"Written to {output}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)


@app.command()
def run(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    evals: Optional[Path] = typer.Option(None, "--evals", help="Path to evals.json"),
    runs: int = typer.Option(3, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    scoring: str = typer.Option("hybrid", "--scoring", help="Scoring mode: hybrid, programmatic, or llm"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    no_calibration: bool = typer.Option(False, "--no-calibration", help="Skip scoring calibration"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    format: str = typer.Option("table", "--format", help="Output format"),
):
    """Run A/B evaluation of a Skill."""
    from skill_eval.evals.loader import EvalLoader
    _check_claude_or_exit()

    if security_gate:
        from skill_eval.security.vetter import SecurityVetter
        vet_report = SecurityVetter(skip_external=False).vet(skill_path)
        _render_security_report(vet_report, console)
        if vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
            raise typer.Exit(code=1)

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
        output_dir=output_dir,
        fmt=format,
    )


@app.command(name="optimize-trigger")
def optimize_trigger(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    eval_set: Path = typer.Option(..., "--eval-set", help="Path to trigger eval set JSON"),
    max_iterations: int = typer.Option(5, "--max-iterations", help="Max optimization iterations"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
):
    """Optimize Skill trigger description."""
    from skill_eval.claude_version import check_claude_version
    from skill_eval.models import LoopConfig, TriggerQuery
    from skill_eval.optimization.loop import OptimizationLoop

    ok, ver_msg = check_claude_version()
    if not ok:
        console.print(f"[red]Error:[/red] {ver_msg}")
        raise typer.Exit(code=3)

    try:
        raw = json.loads(eval_set.read_text(encoding="utf-8"))
        queries = [TriggerQuery(**q) for q in raw]
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to load eval set: {exc}")
        raise typer.Exit(code=2)

    config = LoopConfig(max_iterations=max_iterations, model=model)
    loop = OptimizationLoop(config)
    result = loop.run(skill_path, queries)

    if format == "json":
        console.print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        console.print(f"[bold]Optimization complete:[/bold] {result.exit_reason}")
        console.print(f"  Original: {result.original_description[:80]}...")
        console.print(f"  Best:     {result.best_description[:80]}...")
        console.print(f"  Score:    {result.best_score:.0%} ({result.best_score_label})")
        console.print(f"  Iterations: {result.iterations_run}")


@app.command()
def report(
    workspace: Path = typer.Argument(..., help="Path to evaluation workspace"),
    benchmark: Optional[Path] = typer.Option(None, "--benchmark", help="Path to benchmark.json"),
    previous: Optional[Path] = typer.Option(None, "--previous", help="Path to previous benchmark.json"),
    static: Optional[Path] = typer.Option(None, "--static", help="Path to scorecard.json"),
    port: int = typer.Option(8080, "--port", help="Port for HTTP server"),
    open_browser: bool = typer.Option(False, "--open", help="Open browser automatically"),
):
    """Generate HTML report from evaluation results."""
    from skill_eval.reporting.viewer import ReportGenerator

    bench_data = None
    if benchmark and benchmark.exists():
        bench_data = json.loads(benchmark.read_text(encoding="utf-8"))

    scorecard_data = None
    if static and static.exists():
        try:
            scorecard_data = json.loads(static.read_text(encoding="utf-8"))
        except Exception:
            pass

    prev_data = None
    if previous and previous.exists():
        from skill_eval.reporting.viewer import load_previous_iteration

        prev_data = load_previous_iteration(previous.parent) if previous.is_dir() else None
        if prev_data is None:
            try:
                prev_runs_data = json.loads(previous.read_text(encoding="utf-8"))
                prev_data = {"feedback": None, "runs": prev_runs_data.get("runs", [])}
            except Exception:
                pass

    gen = ReportGenerator()

    if not open_browser:
        output_path = workspace / "report.html"
        html = gen.generate_html(workspace, bench_data, previous=prev_data, scorecard=scorecard_data)
        output_path.write_text(html, encoding="utf-8")
        console.print(f"Report written to {output_path}")
    else:
        gen.serve(workspace, port, bench_data, open_browser=True)


@app.command(name="batch-report")
def batch_report(
    workspace: Path = typer.Argument(..., help="Path to batch workspace directory"),
    manifest: Optional[Path] = typer.Option(None, "--manifest", help="Path to manifest.json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output HTML file path"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser"),
):
    """Generate HTML report from batch evaluation results."""
    from skill_eval.reporting.batch_viewer import generate_batch_html

    manifest_data = None
    if manifest and manifest.exists():
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            pass

    html = generate_batch_html(workspace, manifest_data)

    if not output:
        output = workspace / "batch-report.html"

    output.write_text(html, encoding="utf-8")
    console.print(f"Batch report written to {output}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{output.resolve()}")


@app.command(name="import")
def import_data(
    jsonl_path: Path = typer.Argument(..., help="Path to skill-monitor JSONL file"),
    benchmark: Path = typer.Option(..., "--benchmark", help="Path to benchmark.json to augment"),
):
    """Import skill-monitor JSONL data into benchmark."""
    from skill_eval.integration.monitor_importer import MonitorImporter
    from skill_eval.models import BenchmarkData

    if not jsonl_path.exists():
        console.print(f"[red]Error:[/red] JSONL file not found: {jsonl_path}")
        raise typer.Exit(code=2)

    if not benchmark.exists():
        console.print(f"[red]Error:[/red] Benchmark file not found: {benchmark}")
        raise typer.Exit(code=2)

    try:
        bench_data = BenchmarkData.model_validate_json(benchmark.read_text(encoding="utf-8"))
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to parse benchmark.json: {exc}")
        raise typer.Exit(code=2)

    importer = MonitorImporter()
    updated = importer.import_jsonl(jsonl_path, bench_data)

    benchmark.write_text(
        json.dumps(updated.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    console.print(f"[green]Updated[/green] {benchmark} with monitor data from {jsonl_path}")


@app.command(name="full")
def full(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    runs: int = typer.Option(3, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(120, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser"),
    format: str = typer.Option("table", "--format", help="Output format"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
):
    """One-click full evaluation pipeline."""
    from skill_eval.evals.loader import EvalLoader
    from skill_eval.reporting.viewer import ReportGenerator

    _check_claude_or_exit()

    if not output_dir:
        output_dir = Path("output")

    total_steps = 5 if security_gate else 4
    step = 0

    if security_gate:
        step += 1
        console.print(f"[bold]Step {step}/{total_steps}:[/bold] Security vetting...")
        from skill_eval.security.vetter import SecurityVetter
        vet_report = SecurityVetter(skip_external=False).vet(skill_path)
        _render_security_report(vet_report, console)
        if vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
            raise typer.Exit(code=1)

    step += 1
    console.print(f"[bold]Step {step}/{total_steps}:[/bold] Static analysis...")
    try:
        analyzer = StaticAnalyzer()
        scorecard = analyzer.analyze(skill_path)
        console.print(f"  Score: {scorecard.score}/{scorecard.max_score}")
    except SkillEvalError as exc:
        console.print(f"  [yellow]Warning:[/yellow] {exc}")

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
    )

    step += 1
    lc = _log_console(format)
    lc.print(f"[bold]Step {step}/{total_steps}:[/bold] Aggregating and generating report...")

    gen = ReportGenerator()
    report_path = iteration_dir / "report.html"
    gen.write_static(iteration_dir, report_path, benchmark.model_dump(mode="json"))

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
        for run_dir in config_dir.glob("run-*"):
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


@app.command()
def batch(
    manifest_or_dir: Path = typer.Argument(..., help="Path to manifest JSON or skill directory"),
    runs: int = typer.Option(3, "--runs", help="Runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    layers: str = typer.Option("unit,integration,regression", "--layers", help="Test layers to run"),
    baseline: Optional[Path] = typer.Option(None, "--baseline", help="Baseline benchmark.json for regression"),
    fail_fast: bool = typer.Option(False, "--fail-fast", help="Stop on first failure"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    pipeline: Optional[str] = typer.Option(None, "--pipeline", help="Run a specific pipeline by name"),
    pipeline_steps: Optional[str] = typer.Option(None, "--pipeline-steps", help="Comma-separated step names to run (with auto dep resolution)"),
    project: Optional[str] = typer.Option(None, "--project", help="Override project path for pipeline"),
    projects: Optional[str] = typer.Option(None, "--projects", help="Comma-separated project paths to override pipeline projects"),
):
    """Run batch evaluation across multiple Skills."""
    from skill_eval.batch.manifest import ManifestLoader
    from skill_eval.batch.scheduler import BatchScheduler
    from skill_eval.claude_version import check_claude_version
    from skill_eval.models import RunConfig, ScoringConfig

    ok, ver_msg = check_claude_version()
    if not ok:
        console.print(f"[red]Error:[/red] {ver_msg}")
        raise typer.Exit(code=3)
    console.print(f"[dim]{ver_msg}[/dim]")

    loader = ManifestLoader()
    if manifest_or_dir.is_dir():
        manifest = loader.discover(manifest_or_dir)
        base_dir = manifest_or_dir
    else:
        manifest = loader.load(manifest_or_dir)
        base_dir = manifest_or_dir.parent

    if not output_dir:
        output_dir = Path(f"batch-workspace")

    output_dir.mkdir(parents=True, exist_ok=True)

    run_config = RunConfig(runs_per_config=runs, model=model, max_workers=workers, max_turns=max_turns, output_dir=output_dir)
    scoring_config = ScoringConfig(enable_programmatic=True, enable_llm=True, llm_model=model)

    console.print(f"[bold]Batch evaluation:[/bold] {manifest.name} ({len(manifest.skills)} skills)")

    pipeline_steps_list = [s.strip() for s in pipeline_steps.split(",")] if pipeline_steps else None
    project_overrides = [p.strip() for p in projects.split(",")] if projects else ([project] if project else None)

    layer_list = [l.strip() for l in layers.split(",")]
    if "unit" in layer_list:
        batch_progress = _BatchProgressState(len(manifest.skills))

        def on_batch_progress(skill_name, skill_idx, total_skills,
                              eval_name, config_name, eval_idx, total_evals,
                              completed, total, grading, run_duration=0.0):
            batch_progress.update(
                skill_name, skill_idx, total_skills,
                eval_name, config_name, eval_idx, total_evals,
                completed, total, grading, run_duration,
            )

        scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir, on_progress=on_batch_progress)

        with Live(batch_progress, console=console, refresh_per_second=1) as live:
            batch_progress.live = live
            results = scheduler.run_unit_tests(output_dir, fail_fast)

            for skill_name, status in results.items():
                batch_progress.set_skill_result(skill_name, status.value)

        cascade = scheduler.generate_cascade_report(results)

        console.print(f"\n  Passed: {cascade['passed']}, Failed: {cascade['failed']}, Skipped: {cascade['skipped']}")
        if cascade["attributions"]:
            console.print("  Attributions:")
            for skill, attr in cascade["attributions"].items():
                console.print(f"    {skill}: {attr}")

        cascade_path = output_dir / "cascade-report.json"
        cascade_path.write_text(json.dumps(cascade, indent=2, ensure_ascii=False), encoding="utf-8")

        if cascade.get("failed", 0) > 0:
            raise typer.Exit(code=1)

    if "integration" in layer_list:
        console.print(f"[bold]Running integration tests...[/bold]")
        from skill_eval.config.loader import load_config

        scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir)
        project_config = load_config(base_dir)
        int_result = scheduler.run_integration_tests(
            output_dir,
            config=project_config,
            pipeline_name=pipeline,
            pipeline_steps=pipeline_steps_list,
            project_overrides=project_overrides,
        )

        console.print(f"  Integration: {int_result['passed']}/{int_result['total']} passed")
        if int_result["scenarios"]:
            for sc in int_result["scenarios"]:
                icon = "[green]✓[/green]" if sc.get("passed") else "[red]✗[/red]"
                console.print(f"    {icon} {sc.get('name', '?')} ({sc.get('type', '?')})")

        int_path = output_dir / "integration-report.json"
        int_path.write_text(json.dumps(int_result, indent=2, ensure_ascii=False), encoding="utf-8")

        if int_result.get("failed", 0) > 0:
            raise typer.Exit(code=1)

    if "regression" in layer_list:
        console.print(f"[bold]Running regression tests...[/bold]")
        if not baseline:
            console.print("[yellow]Warning:[/yellow] No --baseline provided, skipping regression")
        else:
            scheduler = BatchScheduler(manifest, run_config, scoring_config, base_dir)
            reg_result = scheduler.run_regression_tests(output_dir, baseline)

            console.print(f"  Regression: {reg_result['passed']}/{reg_result['total']} passed")
            for entry in reg_result.get("regressions", []):
                status = entry.get("status", "?")
                if status == "REGRESSED":
                    icon, color = "✗", "red"
                elif status == "IMPROVED":
                    icon, color = "✓", "green"
                else:
                    icon, color = "○", "dim"
                delta_str = f" ({entry['delta']:+.2f})" if entry.get("delta") is not None else ""
                console.print(f"    [{color}]{icon}[/{color}] {entry['skill']}  {status}{delta_str}")

            reg_path = output_dir / "regression-delta.json"
            reg_path.write_text(json.dumps(reg_result, indent=2, ensure_ascii=False), encoding="utf-8")

            if reg_result.get("failed", 0) > 0:
                raise typer.Exit(code=1)


@app.command(name="scan")
def scan(
    skills_dir: Path = typer.Argument(..., help="Root directory containing skill collections"),
    domain: Optional[str] = typer.Option(None, "--domain", help="Filter by domain"),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Filter by type"),
    format: str = typer.Option("table", "--format", help="Output format: table or json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output file path"),
):
    """Scan skills for eval coverage (L0)."""
    from skill_eval.scanning.scanner import scan_evals

    filters = {}
    if domain:
        filters["domain"] = domain
    if type_filter:
        filters["type"] = type_filter

    result = scan_evals(skills_dir, filters=filters)

    if format == "json":
        text = json.dumps(result, indent=2, ensure_ascii=False)
        if output:
            output.write_text(text, encoding="utf-8")
            console.print(f"Written to {output}")
        else:
            console.print(text)
    else:
        table = Table(title=f"Skill Eval Coverage: {skills_dir}")
        table.add_column("Skill", style="bold")
        table.add_column("Domain")
        table.add_column("Type")
        table.add_column("Evals")
        table.add_column("Status")
        for s in result["skills"]:
            status = "[green]✓[/green]" if s["has_evals"] else "[red]✗[/red]"
            table.add_row(
                s["name"][:40],
                s.get("domain") or "—",
                s.get("type") or "—",
                str(s["eval_count"]),
                status,
            )
        console.print(table)
        console.print(f"\nTotal: {result['total_skills']}  With evals: {result['with_evals']}  Missing: {result['without_evals']}")


@app.command(name="generate-config")
def generate_config(
    skills_dir: Path = typer.Argument(..., help="Root directory containing skill collections"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output YAML path (default: <dir>/eval-config.yaml)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model for claude -p"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scan only, don't call LLM"),
):
    """Generate eval-config.yaml from skills directory using LLM."""
    from skill_eval.claude_version import check_claude_version
    from skill_eval.config.generator import ConfigGenerator

    if not dry_run:
        ok, ver_msg = check_claude_version()
        if not ok:
            console.print(f"[red]Error:[/red] {ver_msg}")
            raise typer.Exit(code=3)

    gen = ConfigGenerator()
    out_path = output or skills_dir / "eval-config.yaml"

    try:
        result = gen.generate(skills_dir, output_path=out_path, model=model, dry_run=dry_run)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if dry_run:
        meta = result.get("metadata", {})
        console.print(f"[bold]Dry run:[/bold] {meta.get('total_skills', 0)} skills found")
        console.print(f"  Domains: {meta.get('domains', [])}")
        console.print(f"  Types: {meta.get('types', [])}")
        console.print(f"  Prefixes: {meta.get('collection_prefixes', [])}")
        console.print(f"  Prompt length: {result.get('prompt_length', 0)} chars")
    else:
        domains = list(result.get("domain_templates", {}).keys())
        types = list(result.get("type_templates", {}).keys())
        console.print(f"Config written to: {out_path}")
        console.print(f"  Domains: {domains}")
        console.print(f"  Types: {types}")
        console.print(f"  Integration scenarios: {len(result.get('integration_scenarios', []))}")
        console.print(f"  Skill keywords: {len(result.get('skill_keywords', {}))}")
        console.print(f"  Pipelines: {len(result.get('pipelines', []))}")
