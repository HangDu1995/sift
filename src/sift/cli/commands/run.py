from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from sift.cli.helpers import _check_claude_or_exit, _discover_evals, _parse_eval_ids, _parse_eval_samples
from sift.cli.main import app, console
from sift.cli.pipeline import _run_ab_eval
from sift.env_check import check_skill_env, check_eval_env
from sift.errors import SiftError
from sift.evals.baseline import skill_name_mismatch
from sift.execution.live_runner import run_live_task
from sift.parser import parse_skill_md, resolve_skill_dir


def _format_run_summary(run_debug: dict) -> str:
    """格式化单次 live run 的 console 摘要行(从 run_debug.json dict)。纯函数(可单测,不 spawn cc)。

    run 命令(真任务)跑完逐 run 打印,让 operational metrics(duration/tokens/turns/stop/tool_calls)
    + 结果头部不再静默孤儿(§50:run_debug 写出后零消费者)。tool_calls 是 dict[str,int]
    (工具名→次数),摘要取 sum(values)=总调用次数。
    """
    run_num = run_debug.get("run_num", "?")
    duration_ms = run_debug.get("duration_ms") or 0
    num_turns = run_debug.get("num_turns") or 0
    total_tokens = run_debug.get("total_tokens") or 0
    stop_reason = run_debug.get("stop_reason") or "—"
    tool_calls = run_debug.get("tool_calls")
    if isinstance(tool_calls, dict):
        tc = sum(int(v) for v in tool_calls.values())
    elif isinstance(tool_calls, list):
        tc = len(tool_calls)
    elif isinstance(tool_calls, (int, float)):
        tc = int(tool_calls)
    else:
        tc = 0
    line = (
        f"run-{run_num}: 耗时 {duration_ms / 1000:.1f}s · "
        f"{num_turns} turns · {total_tokens} tokens · "
        f"stop: {stop_reason} · {tc} tool_calls"
    )
    result = (run_debug.get("result_text") or "").strip()
    if result:
        flat = result.replace("\n", " ").strip()
        if len(flat) > 200:
            flat = flat[:199] + "…"
        line += f"\n      结果: {flat}"
    return line


def _emit_static_and_vet(
    skill_path: Path,
    iteration_dir: Path,
    *,
    skip_static: bool = False,
    skip_vet: bool = False,
    kind: str = "skill",
) -> None:
    """eval 末尾:把静态评分卡 + 安全审查写进 iteration_dir(供 report 面板自动带上)。

    生产者(eval)产齐所有维度的评测产物、消费者(report/gate)只读 —— 让 report 的
    "评分卡/安全"面板默认有数,不再依赖用户手动 analyze/vet -o。analyze/vet 秒级零 API
    成本、纯本地,末尾追加开销可忽略;vet 用原生扫描器(skip_external)保零依赖秒级。

    失败不阻断 eval:两者是补充维度,挂了只让 report 缺那块,不影响动态评测(各自独立)。

    agent 模式跳过:scorecard(11 项 SKILL.md 结构检查)+ vet(structure-check)的检查
    对象是 SKILL.md 目录结构,对 agent.md(单文件)跑出假信号(vet 误报 'Missing SKILL.md'
    → BLOCKED、scorecard 因非 SKILL.md 结构 FAIL)。agent 的静态质量由 critique --kind agent
    覆盖,不靠这俩。
    """
    if kind == "agent":
        return
    if not skip_static:
        try:
            from sift.static_analysis.analyzer import StaticAnalyzer
            report = StaticAnalyzer().analyze(skill_path)
            (iteration_dir / "scorecard.json").write_text(
                json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            console.print(
                f"  [dim]静态评分卡 → {iteration_dir / 'scorecard.json'} "
                f"({report.score}/{report.max_score})[/dim]"
            )
        except Exception as exc:  # 补充维度,不阻断
            console.print(f"[yellow]Warning:[/yellow] 静态分析失败(已跳过,不阻断): {exc}")

    if not skip_vet:
        try:
            from sift.security.vetter import SecurityVetter
            vet = SecurityVetter(skip_external=True).vet(skill_path)
            (iteration_dir / "vet-report.json").write_text(
                json.dumps(vet.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            console.print(
                f"  [dim]安全审查 → {iteration_dir / 'vet-report.json'} (verdict: {vet.verdict})[/dim]"
            )
        except Exception as exc:  # 补充维度,不阻断
            console.print(f"[yellow]Warning:[/yellow] 安全审查失败(已跳过,不阻断): {exc}")


def _emit_report(iteration_dir: Path, *, skip_report: bool = False) -> None:
    """eval 末尾:聚合 benchmark/scorecard/vet 产自包含 report.html(best-effort,不阻断 eval)。

    必须在 _emit_static_and_vet 之后调(scorecard/vet 是它的输入)。无 benchmark.json 则跳过。
    让用户跑完 eval 即得 HTML,不必再单独 `sift report`。
    """
    if skip_report:
        return
    try:
        from sift.cli.commands.report import generate_report_html
        html_path = generate_report_html(iteration_dir)
        if html_path:
            console.print(f"  [dim]HTML 报告 → {html_path}[/dim]")
    except Exception as exc:  # 补充维度,不阻断
        console.print(f"[yellow]Warning:[/yellow] HTML 报告生成失败(已跳过,不阻断): {exc}")


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


def _run_security_gate(skill_path: Path) -> None:
    """--security-gate:跑 skill 前全量安全审查,BLOCKED/REVIEW_NEEDED → exit 1。

    一处定义,eval/run 各调一次(调用点保留 `if security_gate:` 判断),避免两处复制漂移。
    """
    from sift.security.vetter import SecurityVetter
    vet_report = SecurityVetter(skip_external=False).vet(skill_path)
    _render_security_report(vet_report, console)
    if vet_report.verdict in ("BLOCKED", "REVIEW_NEEDED"):
        raise typer.Exit(code=1)


def _resolve_skip_compliance(no_compliance: bool | None) -> bool:
    """compliance 默认开:单 transcript 过程对账本就有效(与独立 audit 同引擎、审 cc 已产出的
    transcript、不 re-spawn cc),注入声明即跑。--no-compliance 显式跳过(如快速冒烟测试)。"""
    return no_compliance if no_compliance is not None else False


@app.command(rich_help_panel="评测")
def eval(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    evals: Optional[Path] = typer.Option(None, "--evals", help="Path to evals.json"),
    runs: int = typer.Option(1, "--runs", help="Number of runs per configuration"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    workers: int = typer.Option(4, "--workers", help="Max parallel workers"),
    timeout: int = typer.Option(300, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    max_retries: int = typer.Option(2, "--max-retries", help="Max retries per eval on UNEVAL/error (each retry re-runs claude + scoring + compliance LLM; heavy skills may want 0-1 to save tokens)"),
    scoring: str = typer.Option("hybrid", "--scoring", help="Scoring mode: hybrid, programmatic, or llm"),
    no_compliance: Optional[bool] = typer.Option(None, "--no-compliance/--compliance", help="Skip compliance checking (default: on)"),
    refine_instructions: bool = typer.Option(True, "--refine-instructions/--no-refine-instructions", help="精筛(默认开):LLM 砍掉 extractor 误抽的示例/占位伪指令(audit/compliance 提精度,精度>召回);--no-refine-instructions 关闭"),
    generate_headline: bool = typer.Option(True, "--headline/--no-headline", help="人话标题(默认开):独立 LLM 步骤把每条指令的原文 bullet 去噪+补主语压成一句显示标题(判定不读);--no-headline 关闭省那次 LLM"),
    no_calibration: bool = typer.Option(False, "--no-calibration", help="Skip scoring calibration"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
    no_static: bool = typer.Option(False, "--no-static", help="跳过末尾自动产静态评分卡(默认产,供 report 评分卡面板)"),
    no_vet: bool = typer.Option(False, "--no-vet", help="跳过末尾自动产安全审查(默认产,供 report 安全面板)"),
    no_report: bool = typer.Option(False, "--no-report", help="跳过末尾自动产 HTML 报告(默认产 report.html)"),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    format: str = typer.Option("table", "--format", help="Output format"),
    eval_samples: Optional[str] = typer.Option(None, "--eval-samples", help="Number or pct of evals to run (e.g. 10 or 0.25)"),
    eval_ids: Optional[str] = typer.Option(None, "--eval-ids", help="只跑指定 id 的题(逗号分隔,如 3,5,7);和 --eval-samples 互斥"),
    changed_from: Optional[Path] = typer.Option(
        None, "--changed-from",
        help="改 skill 后只跑受影响的题:给基线 skill 目录,自动 diff 指令 + LLM 判受影响 eval(回归探针;和 --eval-ids/--eval-samples 互斥)。子集 delta 非整体测量,周期仍需全量兜底",
    ),
    skill_b: Optional[Path] = typer.Option(
        None, "--skill-b",
        help="v1/v2 模式:第二个 skill 路径(两边都注入各自 skill);不给则默认 with/without(同一 skill 开/关)",
    ),
    label_a: Optional[str] = typer.Option(None, "--label-a", help="v1/v2 模式 side A 显示标签(默认 skill 目录 basename)"),
    label_b: Optional[str] = typer.Option(None, "--label-b", help="v1/v2 模式 side B 显示标签(默认 skill 目录 basename)"),
    kind: str = typer.Option("skill", "--kind", help="声明类型:skill(默认,A/B 对照)/ agent(绝对质量,--append-system-prompt 注入 agent 正文;需配 --evals)"),
):
    """A/B 评测 skill(with/without 或 v1/v2);--kind agent 走绝对质量评测。"""
    skill_path = resolve_skill_dir(skill_path)
    from sift.evals.loader import EvalLoader
    if kind not in ("skill", "agent"):
        console.print(f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {kind!r}")
        raise typer.Exit(code=2)
    if kind == "agent" and skill_b:
        console.print("[red]Error:[/red] --kind agent 是绝对质量评测,与 --skill-b(A/B 对照)互斥")
        raise typer.Exit(code=2)
    # A/B 评测恒用 cc
    _check_claude_or_exit()

    if security_gate:
        _run_security_gate(skill_path)

    evals_path = evals or _discover_evals(skill_path)
    if not evals_path:
        return

    try:
        loader = EvalLoader()
        eval_set = loader.load(evals_path)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    # 冻结基线 skill_name 轻校验:baseline 是 skill-A 的却考 skill-B → 警告(不阻断)。
    # 普通 evals.json 无 sidecar → 不校验;同 skill 不同版本(skill_name 相同)放行。
    try:
        _running_name = parse_skill_md(skill_path)[0]
    except Exception:
        _running_name = ""
    _baseline_warn = skill_name_mismatch(evals_path, _running_name)
    if _baseline_warn:
        console.print(f"[yellow]Warning:[/yellow] {_baseline_warn}")

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

    # compliance 默认开(注入声明即跑):单 transcript 过程对账本就有效(与独立 audit 同引擎、
    # 审 cc 已产出的 transcript、不 re-spawn cc)。--no-compliance 跳过(如快速冒烟测试)。
    skip_compliance = _resolve_skip_compliance(no_compliance)

    _benchmark, iteration_dir = _run_ab_eval(
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
        refine_instructions=refine_instructions,
        generate_headline=generate_headline,
        output_dir=output_dir,
        fmt=format,
        eval_samples=_parse_eval_samples(eval_samples),
        eval_ids=_parse_eval_ids(eval_ids),
        changed_from=changed_from,
        max_retries=max_retries,
        skill_b=skill_b,
        label_a=label_a,
        label_b=label_b,
        kind=kind,
    )

    # 末尾自动产静态评分卡 + 安全审查(写进 iteration_dir,供 report 面板自动带上)
    _emit_static_and_vet(skill_path, iteration_dir, skip_static=no_static, skip_vet=no_vet, kind=kind)
    # 再聚合上面三者产 report.html(必须在 static/vet 之后:它们是输入)
    _emit_report(iteration_dir, skip_report=no_report)


@app.command(rich_help_panel="真任务")
def run(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    task: str = typer.Option(
        ..., "--task",
        help="真任务文本:直接 spawn 执行器跑此真实任务(不读 evals.json);产物 run-*/transcript.{jsonl,json}(cc .jsonl / opencode .json)可喂 audit",
    ),
    cwd: Optional[Path] = typer.Option(
        None, "--cwd",
        help="真任务工作目录(任务材料真实文件);拷贝隔离,cc 不碰原目录",
    ),
    runs: int = typer.Option(1, "--runs", help="每任务运行次数"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for claude -p"),
    timeout: int = typer.Option(300, "--timeout", help="Timeout per run in seconds"),
    max_turns: int = typer.Option(10, "--max-turns", help="Max agentic turns per claude -p run"),
    executor: Optional[str] = typer.Option(
        None, "--executor",
        help="执行器:cc(默认)/ opencode / cc,opencode(多执行器各跑一次)。注意 opencode 无 --max-turns 对等,多执行器 sweep 时 cc 轮数有界、opencode 无界(已知不对称)",
    ),
    output_dir: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory"),
    security_gate: bool = typer.Option(False, "--security-gate", help="Fail if security vetting returns BLOCKED or REVIEW_NEEDED"),
):
    """对 skill 跑真任务:spawn 执行器执行、dump trace 喂 audit(不读 evals.json)。"""
    skill_path = resolve_skill_dir(skill_path)
    # 执行器解析(提前到 cc-check 前,好按需 gate)
    exec_list = [e.strip() for e in (executor or "cc").split(",") if e.strip()]
    bad = [e for e in exec_list if e not in ("cc", "opencode")]
    if bad:
        console.print(f"[red]Error:[/red] 未知 executor: {', '.join(bad)}(支持: cc, opencode)")
        raise typer.Exit(code=2)
    # cc-check:cc 在 exec_list 时才需要(纯 opencode 不用 cc)
    if "cc" in exec_list:
        _check_claude_or_exit()

    if security_gate:
        _run_security_gate(skill_path)

    if not output_dir:
        output_dir = Path("output")
    try:
        run_live_task(
            skill_path=skill_path,
            task=task,
            cwd=cwd,
            runs=runs,
            model=model,
            timeout=timeout,
            max_turns=max_turns,
            output_dir=output_dir,
            on_run_progress=lambda line: console.print(line),
            executors=exec_list,
        )
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
    transcripts = sorted(output_dir.glob("run-*/transcript.*"))
    console.print(f"\n[bold]Live task:[/bold] {len(exec_list)} executor(s) × {runs} run(s) → {len(transcripts)} transcript(s)")
    # 每 run 的 operational 摘要(读 run-N/run_debug.json;解 §50 "run_debug 孤儿")
    for debug_path in sorted(output_dir.glob("run-*/run_debug.json")):
        try:
            console.print(_format_run_summary(json.loads(debug_path.read_text(encoding="utf-8"))))
        except Exception:
            pass
    for t in transcripts:
        console.print(f"  [dim]{t}[/dim]")
    if transcripts:
        args = " ".join(f"--transcript {t}" for t in transcripts)
        console.print(
            f"\n[dim]Audit skill 遵循性:[/dim]\n  sift audit {skill_path} {args} -o /tmp/live-audit"
        )
