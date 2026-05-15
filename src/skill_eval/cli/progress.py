from __future__ import annotations

import sys
import time

from rich.panel import Panel


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

        import sys as _sys
        progress_line = self._format_progress_line()
        if progress_line:
            if _sys.stderr.isatty():
                lines = str(self._last_line_count or 1)
                _sys.stderr.write(f"\033[{lines}A\033[J")
            _sys.stderr.write(progress_line + "\n")
            _sys.stderr.flush()
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
