from __future__ import annotations

import sys
import threading
import time

from rich.panel import Panel


def _config_label(config_name: str) -> str:
    """side 名 → 短标签(进度行用)。默认模式 with/without;v1/v2 用各自 side key。"""
    if config_name == "with_skill":
        return "with"
    if config_name == "without_skill":
        return "BASE"
    return (config_name or "?")[:14]


class _ProgressState:
    """run 进度渲染:顶部 live bar(原地更新)+ 每次执行完成追加持久日志行。

    设计:每次 spawn(cc 跑一条 eval 的一个 side)= 一次「执行」,共
    ``num_sides * runs * num_evals`` 次(默认 2 臂 / agent 单臂 num_sides=1)。每次执行的 config(with/BASE/side)、题名、判卷结果
    (通过率 + 失败项,失败项标客观/主观)单独占一行、**持久保留**(不再被覆写),
    滚动累积成一条条执行记录;顶部一行原地更新的进度条给整体 % / ETA / 当前在跑的执行。

    线程安全:workers 并发,on_progress 多线程交错调 update → 用锁串行化渲染,
    避免持久行与 live 块擦写交错撕裂。

    触发持久行的判据:**``completed`` 计数自增**(runner 在 skipped/failed/完成时自增,
    retrying 不自增)。三类完成事件都带 grading(skipped 带既有、failed 带错误 grading、
    完成带 overall),故按 completed 自增即可精确捕获「一次执行结束」、自动跳过 retry。
    """

    def __init__(self, num_evals: int, runs_per_config: int, num_sides: int = 2):
        self.num_evals = num_evals
        self.runs_per_config = runs_per_config
        self.total_runs = num_sides * runs_per_config * num_evals
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.retried = 0
        # live(在跑)状态
        self.current_eval = ""
        self.current_config = ""
        self.eval_idx = 0
        self.phase = ""
        self.substep = ""
        self.run_start_time: float | None = None  # executing 起算,跨 scoring/compliance 保持,完成清
        # 计时(avg/ETA)
        self.total_elapsed = 0.0
        self._timed_count = 0
        # 渲染簿记
        self._logged_completed = 0   # 已打印持久行的最高 completed 值
        self._last_live_lines = 0    # 当前 live 块占几行(擦写用)
        self._lock = threading.Lock()

    def update(self, eval_name, config_name, eval_idx, total_evals, completed, total,
               grading, run_duration=0.0, run_output=None, phase="", substep=""):
        with self._lock:
            persistent = self._apply(
                eval_name, config_name, eval_idx, completed, total,
                grading, run_duration, phase, substep,
            )
            self._render(persistent)

    def _apply(self, eval_name, config_name, eval_idx, completed, total,
               grading, run_duration, phase, substep):
        """更新计数/状态,返回本次新增的持久行(完成时)或 []。须持锁调。"""
        if phase == "skipped":
            self.skipped += 1
        elif phase == "failed":
            self.failed += 1
        elif phase == "retrying":
            self.retried += 1

        if completed > self._logged_completed:
            # 一次执行完成(skipped/failed/done;retrying 不自增 completed 故不进这)
            self._logged_completed = completed
            self.completed = completed
            persistent = []
            if grading is not None:
                persistent = self._completion_lines(
                    eval_name, config_name, completed, total, grading, run_duration, phase, substep,
                )
            if run_duration:
                self.total_elapsed += run_duration
                self._timed_count += 1
            self.run_start_time = None
            self.phase = ""
            return persistent

        # in-flight(executing/scoring/compliance/retrying):只更新 live 状态
        self.current_eval = eval_name
        self.current_config = config_name
        self.eval_idx = eval_idx
        self.phase = phase
        self.substep = substep
        if phase == "executing":
            self.run_start_time = time.time()  # 新执行起算;scoring/compliance 不重置(保留 executing 起算)
        return []

    def _completion_lines(self, eval_name, config_name, idx, total,
                          grading, dur, phase, substep):
        """一次执行完成的持久行:config/题名/通过率/耗时 + 失败项(标客观/主观)。"""
        label = _config_label(config_name)
        name = (eval_name or "")[:28]
        if phase == "failed":
            extra = f"({substep}) " if substep else ""
            return [f"✗ {idx}/{total} {name} · {label} · 执行失败 {extra}· {dur:.0f}s"]
        s = grading.summary
        icon = "✓" if s.pass_rate >= 0.5 else "✗"
        lines = [f"{icon} {idx}/{total} {name} · {label} · "
                 f"{int(s.pass_rate * 100)}% ({s.passed}/{s.total}) · {dur:.0f}s"]
        failed = [e for e in (grading.expectations or []) if not e.passed]
        for fe in failed[:3]:
            tag = "客观" if getattr(fe, "source", "") == "programmatic" else "主观"
            lines.append(f"     ✗ [{tag}] {(fe.text or '')[:56]}")
        if len(failed) > 3:
            lines.append(f"     …还有 {len(failed) - 3} 条未通过")
        return lines

    def _format_live(self):
        """顶部 live 块:bar 行 + (有执行在跑时)当前执行行。返回不含尾换行的多行串。"""
        if self.total_runs == 0:
            return ""
        pct = self.completed / self.total_runs * 100
        filled = int(20 * pct / 100)
        bar = "█" * filled + "░" * (20 - filled)
        parts = [f"[{bar}] {pct:.0f}% ({self.completed}/{self.total_runs})"]
        counters = []
        if self.skipped:
            counters.append(f"skip:{self.skipped}")
        if self.failed:
            counters.append(f"fail:{self.failed}")
        if self.retried:
            counters.append(f"retry:{self.retried}")
        if counters:
            parts.append("·".join(counters))
        if self._timed_count > 0:
            avg = self.total_elapsed / self._timed_count
            eta = (self.total_runs - self.completed) * avg
            parts.append(f"avg {avg:.0f}s ETA {eta / 60:.0f}m{eta % 60:02.0f}s")
        lines = [" · ".join(parts)]
        if self.run_start_time is not None and self.current_eval:
            elapsed = time.time() - self.run_start_time
            label = _config_label(self.current_config)
            ph = f"({self.phase})" if self.phase and self.phase != "executing" else ""
            sub = f" {self.substep}" if self.substep else ""
            tail = f"{ph}{sub}".rstrip()
            lines.append(f"  ⏳ {label} · {self.current_eval[:24]} · {elapsed:.0f}s{(' ' + tail) if tail else ''}")
        return "\n".join(lines)

    def _render(self, persistent):
        """擦旧 live 块 → 打新持久行(留)→ 重画 live 块。非 tty 只流式打持久行。"""
        is_tty = sys.stderr.isatty()
        if is_tty:
            if self._last_live_lines:
                sys.stderr.write(f"\033[{self._last_live_lines}A\033[J")
            for ln in persistent:
                sys.stderr.write(ln + "\n")
            live = self._format_live()
            if live:
                sys.stderr.write(live + "\n")
            sys.stderr.flush()
            self._last_live_lines = (live.count("\n") + 1) if live else 0
        else:
            # 非 tty(管道/日志):原地 bar 会把 ANSI 转义码灌进去,只流式打持久行。
            for ln in persistent:
                sys.stderr.write(ln + "\n")
            sys.stderr.flush()
            self._last_live_lines = 0


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
