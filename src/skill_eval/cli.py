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


def _parse_eval_samples(val: str | None) -> int | float | None:
    if not val:
        return None
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        return None


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
    eval_samples: int | float | None = None,
    per_eval_mode: bool = True,
) -> tuple:
    from skill_eval.execution.runner import ABTestRunner
    from skill_eval.execution.benchmark import BenchmarkAggregator
    from skill_eval.models import RunConfig, ScoringConfig

    lc = _log_console(fmt)
    total_evals = len(eval_set.evals)
    sampled = ""
    if eval_samples:
        if isinstance(eval_samples, float) and eval_samples <= 1.0:
            sampled = f" (sampled {int(eval_samples * 100)}%)"
        else:
            sampled = f" (sampled {eval_samples}/{total_evals})"
    lc.print(f"Running A/B test: {eval_set.skill_name} ({total_evals} evals{sampled} × {runs} runs, {'per-eval' if per_eval_mode else 'merged'} mode)")

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
        eval_samples=eval_samples,
        per_eval_mode=per_eval_mode,
        per_eval_timeout=timeout,
        min_runs_for_stats=max(3, runs),
    )

    progress_state = _ProgressState(len(eval_set.evals), runs)

    def on_progress(eval_name, config_name, eval_idx, total_evals, completed, total, grading, run_duration=0.0, run_output=None, phase="", substep=""):
        progress_state.update(eval_name, config_name, eval_idx, completed, total, grading, run_duration, run_output, phase=phase, substep=substep)

    try:
        ab = ABTestRunner(run_config, scoring_config, on_progress=on_progress)
        iteration_dir = ab.run_ab_test(skill_path, eval_set)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    agg = BenchmarkAggregator(model=model)
    try:
        benchmark = agg.aggregate(iteration_dir, skill_path)
    except Exception as exc:
        console.print(f"[red]Error:[/red] Benchmark aggregation failed: {exc}")
        console.print(f"  iteration_dir: {iteration_dir}")
        configs = [d.name for d in iteration_dir.iterdir() if d.is_dir()]
        console.print(f"  config dirs found: {configs}")
        for c in configs:
            cd = iteration_dir / c
            runs_direct = list(cd.glob("run-*"))
            runs_nested = list(cd.glob("eval-*/run-*"))
            console.print(f"    {c}/: {len(runs_direct)} direct runs, {len(runs_nested)} nested runs")
        raise typer.Exit(code=1)

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
        self.total_runs = 2 * runs_per_config * num_evals
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.retried = 0
        self.current_eval = ""
        self.current_config = ""
        self.eval_idx = 0
        self.last_grading = None
        self.results: list[dict] = []
        self.run_start_time: float | None = None
        self.last_run_duration: float = 0.0
        self.total_elapsed: float = 0.0
        self.phase: str = ""
        self.substep: str = ""
        self._last_line_count: int = 0

    def update(self, eval_name, config_name, eval_idx, completed, total, grading, run_duration=0.0,
                run_output=None, phase="", substep=""):
        self.current_eval = eval_name
        self.current_config = config_name
        self.eval_idx = eval_idx
        self.phase = phase
        self.substep = substep
        if phase == "skipped":
            self.skipped += 1
        if phase == "failed":
            self.failed += 1
        if phase == "retrying":
            self.retried += 1
        prev_completed = self.completed
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

        import sys
        progress_line = self._format_progress_line()
        if progress_line:
            if sys.stderr.isatty():
                lines = str(self._last_line_count or 1)
                sys.stderr.write(f"\033[{lines}A\033[J")
            sys.stderr.write(progress_line + "\n")
            sys.stderr.flush()
            self._last_line_count = progress_line.count("\n") + 1

    def _format_progress_bar(self, width: int, pct: float) -> str:
        filled = int(width * pct / 100) if width else 0
        return "█" * filled + "░" * (width - filled)

    def _format_progress_line(self) -> str:
        if self.total_runs == 0:
            return ""
        pct = self.completed / self.total_runs * 100
        bar = self._format_progress_bar(20, pct)
        parts = [f"[{bar}] {pct:.0f}% ({self.completed}/{self.total_runs})"]

        counters = []
        if self.skipped:
            counters.append(f"skip:{self.skipped}")
        if self.failed:
            counters.append(f"fail:{self.failed}")
        if self.retried:
            counters.append(f"retry:{self.retried}")
        if counters:
            parts.append("|".join(counters))

        if self.current_eval:
            tag = "SKILL" if self.current_config == "with_skill" else "BASE"
            if self.run_start_time is not None:
                elapsed = time.time() - self.run_start_time
                phase_str = f"[{self.phase}]" if self.phase else ""
                parts.append(f"  [{self.eval_idx}/{self.num_evals}] {self.current_eval[:25]} {tag} ⏳ {elapsed:.0f}s {phase_str} {self.substep}")
            elif self.last_grading is not None:
                r = self.results[-1]
                icon = "✓" if r.get("pass_rate", 0) >= 0.5 else "✗"
                dur = f"{r.get('duration', 0):.0f}s"
                parts.append(f"  {icon} {self.current_eval[:25]} {tag} {int(r.get('pass_rate',0)*100)}% [{dur}]")
            else:
                parts.append(f"  [{self.eval_idx}/{self.num_evals}] {self.current_eval[:25]} {tag}")

        if self.results and self.total_elapsed > 0:
            avg = self.total_elapsed / len(self.results)
            remaining = self.total_runs - self.completed
            eta_s = remaining * avg
            parts.append(f" | avg {avg:.0f}s ETA {eta_s/60:.0f}m{eta_s%60:02.0f}s")

        result_lines = [" ".join(parts)]

        if self.results:
            r = self.results[-1]
            if r.get("failed_expectations"):
                for fe in r["failed_expectations"][:2]:
                    result_lines.append(f"    ✗ {fe[:80]}")

        return "\n".join(result_lines)


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

    vet_output = output or skill_path / "vet-report.json"
    if not vet_output.suffix == ".json":
        vet_output = vet_output / "vet-report.json"
    vet_output.parent.mkdir(parents=True, exist_ok=True)
    vet_output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if format != "json":
        console.print(f"  Report: {vet_output}")

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
    source_dir: Optional[Path] = typer.Option(None, "--source-dir", help="Path to source project directory for diverse eval generation"),
    domains: Optional[str] = typer.Option(None, "--domains", help="Comma-separated target domains for diverse eval generation"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for LLM generation"),
    num_evals: Optional[int] = typer.Option(None, "--num-evals", help="Max number of eval cases to generate"),
):
    """Initialize evals.json for a Skill.

    Without --source-dir: generates L1 unit test cases from SKILL.md content.
    With --source-dir: generates diverse eval cases from domains distinct from the source project,
    suitable for overfitting detection.
    """
    if source_dir:
        if not source_dir.is_dir():
            console.print(f"[red]Error:[/red] source-dir not found: {source_dir}")
            raise typer.Exit(code=2)

        from skill_eval.overfitting.source_analyzer import SourceAnalyzer
        from skill_eval.evals.diverse_generator import DiverseEvalGenerator

        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            console.print(f"[red]Error:[/red] SKILL.md not found in {skill_path}")
            raise typer.Exit(code=2)

        console.print(f"Scanning source project: {source_dir} ...")
        analyzer = SourceAnalyzer()
        skill_hints = [skill_path.name] if skill_path.is_dir() else [skill_path.stem]
        source_profile = analyzer.analyze(source_dir, skill_hints=skill_hints)
        console.print(f"  {source_profile.project_summary}")

        domain_list = [d.strip() for d in domains.split(",") if d.strip()] if domains else None
        if domain_list:
            console.print(f"Target domains: {', '.join(domain_list)}")

        console.print("Generating diverse eval cases with LLM ...")
        gen = DiverseEvalGenerator(model=model)
        eval_set = gen.generate(skill_path, source_profile, domains=domain_list)

        if output is None:
            evals_dir = skill_path / "evals"
            evals_dir.mkdir(exist_ok=True)
            output = evals_dir / "evals-general.json"
        elif output.is_dir():
            output = output / "evals-general.json"

        data = eval_set.model_dump(mode="json")
        output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        console.print(f"Generated [green]{len(eval_set.evals)}[/green] diverse eval cases for [bold]{eval_set.skill_name}[/bold]")
        for e in eval_set.evals:
            level = getattr(e, 'level', 'L1')
            extras = []
            if e.fixtures:
                extras.append(f"{len(e.fixtures)} fixtures")
            console.print(f"  [{level}] {e.name}: {len(e.expectations)} expectations" +
                          (f", {', '.join(extras)}" if extras else ""))
        console.print(f"Written to {output}")
        return

    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=None, timeout=300)

        if generate_prompts:
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
    timeout: int = typer.Option(300, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    scoring: str = typer.Option("hybrid", "--scoring", help="Scoring mode: hybrid, programmatic, or llm"),
    compliance_gate: bool = typer.Option(False, "--compliance-gate", help="Force-fail if compliance_rate < 0.5"),
    no_calibration: bool = typer.Option(False, "--no-calibration", help="Skip scoring calibration"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    format: str = typer.Option("table", "--format", help="Output format"),
    eval_samples: Optional[str] = typer.Option(None, "--eval-samples", help="Number or pct of evals to run (e.g. 10 or 0.25)"),
    merged: bool = typer.Option(False, "--merged", help="Use merged-prompt mode instead of per-eval"),
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

    if runs < 2:
        console.print(
            "[yellow]Warning:[/yellow] --runs 1 means no statistical validity. "
            "Single-run results have high variance and outlier detection disabled. "
            "Use --runs 3+ for reliable results."
        )

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
        eval_samples=_parse_eval_samples(eval_samples),
        per_eval_mode=not merged,
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
    _check_claude_or_exit()
    from skill_eval.models import LoopConfig, TriggerQuery
    from skill_eval.optimization.loop import OptimizationLoop

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
    scorecard: Optional[Path] = typer.Option(None, "--scorecard", "--static", help="Path to scorecard.json"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser after generation"),
):
    """Generate a static HTML report from evaluation results."""
    from skill_eval.reporting.viewer import ReportGenerator

    bench_data = None
    bench_path = benchmark
    if not bench_path or not bench_path.exists():
        for candidate in [workspace / "benchmark.json", workspace / "iteration-1" / "benchmark.json"]:
            if candidate.exists():
                bench_path = candidate
                break
    if bench_path and bench_path.exists():
        bench_data = json.loads(bench_path.read_text(encoding="utf-8"))

    scorecard_path = scorecard
    if not scorecard_path or not scorecard_path.exists():
        for candidate in [workspace / "scorecard.json", workspace / "iteration-1" / "scorecard.json"]:
            if candidate.exists():
                scorecard_path = candidate
                break
    scorecard_data = None
    if scorecard_path and scorecard_path.exists():
        try:
            scorecard_data = json.loads(scorecard_path.read_text(encoding="utf-8"))
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
    output_path = workspace / "report.html"
    gen.write_static(workspace, output_path, bench_data, previous=prev_data, scorecard=scorecard_data)
    console.print(f"Report written to {output_path}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{output_path.resolve()}")


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
    _check_claude_or_exit()
    from skill_eval.batch.manifest import ManifestLoader
    from skill_eval.batch.scheduler import BatchScheduler
    from skill_eval.models import RunConfig, ScoringConfig

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
    from skill_eval.config.generator import ConfigGenerator

    if not dry_run:
        _check_claude_or_exit()

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


@app.command(name="vet-overfit")
def vet_overfit(
    domain_bench: Path = typer.Option(..., "--domain-bench", help="Path to domain benchmark.json"),
    general_bench: Optional[Path] = typer.Option(None, "--general-bench", help="Path to general benchmark.json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output path for overfitting report"),
    fmt: str = typer.Option("table", "--format", help="Output format: table or json"),
):
    """Detect overfitting by comparing domain vs general benchmarks."""
    from skill_eval.overfitting.gap import GapCalculator
    from skill_eval.overfitting.detector import OverfittingDetector

    gap_data = None

    if general_bench and general_bench.exists():
        try:
            domain_data = json.loads(domain_bench.read_text(encoding="utf-8"))
            general_data = json.loads(general_bench.read_text(encoding="utf-8"))
            calc = GapCalculator()
            gap = calc.calculate(domain_data, general_data)
            gap_data = gap.model_dump()
        except Exception as exc:
            console.print(f"[yellow]Warning:[/yellow] Could not calculate generalization gap: {exc}")

    skill_name = ""
    if domain_bench.exists():
        try:
            domain_data = json.loads(domain_bench.read_text(encoding="utf-8"))
            skill_name = domain_data.get("metadata", {}).get("skill_name", "")
        except Exception:
            pass

    detector = OverfittingDetector()
    report = detector.build_report(
        skill_name=skill_name,
        gap_data=gap_data,
    )

    if not output:
        output = domain_bench.parent / "overfitting_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if fmt == "json":
        console.print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        gap = report.generalization_gap
        if gap:
            gap_color = "green" if gap.verdict == "good" else "yellow" if gap.verdict == "warn" else "red"
            console.print("\n[bold]Generalization Gap:[/bold]")
            console.print(f"  Domain:  +{gap.domain_delta:.2f} (ws={gap.domain_pass_rate_with:.0%}, wos={gap.domain_pass_rate_without:.0%})")
            console.print(f"  General: +{gap.general_delta:.2f} (ws={gap.general_pass_rate_with:.0%}, wos={gap.general_pass_rate_without:.0%})")
            console.print(f"  Gap:     [{gap_color}]{gap.gap:.2f}[/{gap_color}] → [{gap_color}]{gap.verdict.upper()}[/{gap_color}]")

        console.print(f"\n[bold]Overfitting Score:[/bold] {report.overfitting_score:.2f}/1.0")
        console.print(f"\n[bold]Recommendation:[/bold] {report.recommendation}")
        console.print(f"\nReport written to: {output}")

    if report.overfitting_score >= 0.7:
        raise typer.Exit(code=1)
