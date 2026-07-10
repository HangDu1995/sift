"""audit 命令:把 TranscriptAuditor.audit 接到命令行。

对账真实会话 transcript 与 SKILL.md 声明的指令(**不 re-run**,直接对账历史执行),
产出 audit-report.json + console summary(照 init/run 的 typer 风格)。

设计要点:
- ``TranscriptAuditor`` 在**模块级** import(不像 init/run 把重件 import 放函数内),
  以便测试用 ``monkeypatch.setattr("skill_eval.cli.commands.audit.TranscriptAuditor", ...)``
  替换;模块级名字解析使函数体内的 ``TranscriptAuditor()`` 在调用时取到被替换后的对象。
- 错误处理照 init/run:``SkillEvalError`` → console.print 红色 + ``typer.Exit(code=exc.exit_code)``。
- console summary 着色 pass=green / fail=red / na=yellow;FAIL/N/A 明细用 rich Table
  (PASS 是关注点外的默认态,太多不列;至少列 FAIL/N/A)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from skill_eval.audit.auditor import TranscriptAuditor
from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.models.audit_models import AuditVerdict
from skill_eval.parser import parse_skill_md

_TRUNCATE_WIDTH = 60


def _truncate(text: str, width: int = _TRUNCATE_WIDTH) -> str:
    """单行化 + 截断,供表格单元格展示(instruction_text / evidence 可能很长)。"""
    flat = text.replace("\n", " ").strip()
    return flat if len(flat) <= width else flat[: width - 1] + "…"


@app.command()
def audit(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    transcript: list[Path] = typer.Option(
        ...,
        "--transcript",
        help="真实会话 transcript jsonl(可重复 --transcript a --transcript b)",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output", "-o", help="输出目录(默认 ./output)"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="语义判定 LLM"),
    format: str = typer.Option("table", "--format", help="table|json"),
):
    """Audit real session transcript(s) against SKILL.md(不 re-run,直接对账真实历史执行)。"""
    try:
        name, _desc, _content, _frontmatter, body = parse_skill_md(skill_path)
        report = TranscriptAuditor().audit(name, body, transcript, model=model)
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)

    out = output_dir or Path("output")
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "audit-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    summary = report.summary
    total = summary.get("total", 0)
    n_pass = summary.get("pass", 0)
    n_fail = summary.get("fail", 0)
    n_na = summary.get("na", 0)

    if format == "json":
        console.print(report.model_dump_json(indent=2))
        console.print(f"Written to {report_path}")
        return

    console.print(
        f"Audited {len(report.transcripts)} transcript(s) for [bold]{name}[/bold]: "
        f"total={total} "
        f"pass=[green]{n_pass}[/green] "
        f"fail=[red]{n_fail}[/red] "
        f"na=[yellow]{n_na}[/yellow]"
    )

    if report.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in report.warnings:
            console.print(f"  [yellow]- {w}[/yellow]")

    # 多 transcript:展示指令级聚合(系统性违规一眼可见,如"NEVER fabricate 在 75% 会话被违反")。
    # 单 transcript 不显示(聚合无额外洞察,每指令 1 verdict,平铺明细已够)。
    if len(report.transcripts) > 1 and report.by_instruction:
        agg = Table(title=f"By instruction (across {len(report.transcripts)} transcripts)")
        agg.add_column("fail%", style="bold")
        agg.add_column("instruction")
        agg.add_column("category")
        agg.add_column("fail/total")
        agg.add_column("fail in transcripts")
        for row in report.by_instruction:
            # 全 PASS 的不列(默认正常态,太多);只列有 fail 或 na 的(关注点)。
            if row.fail_count == 0 and row.na_count == 0:
                continue
            color = "red" if row.fail_rate >= 0.5 else "yellow"
            agg.add_row(
                f"[{color}]{row.fail_rate * 100:.0f}%[/{color}]",
                _truncate(row.instruction_text),
                row.category.value,
                f"{row.fail_count}/{row.total}",
                ", ".join(row.fail_in_transcripts) if row.fail_in_transcripts else "—",
            )
        console.print(agg)

    # 只列 FAIL/N/A 明细(PASS 是默认正常态,太多;FAIL/N/A 才是关注点)。
    non_pass = [f for f in report.findings if f.verdict != AuditVerdict.PASS]
    if non_pass:
        table = Table(title="Findings (FAIL / N/A)")
        table.add_column("verdict", style="bold")
        table.add_column("category")
        table.add_column("instruction_text")
        table.add_column("transcript")
        table.add_column("evidence")
        for f in non_pass:
            color = "red" if f.verdict == AuditVerdict.FAIL else "yellow"
            table.add_row(
                f"[{color}]{f.verdict.value}[/{color}]",
                f.category.value,
                _truncate(f.instruction_text),
                f.transcript,
                _truncate(f.evidence),
            )
        console.print(table)

    console.print(f"Written to {report_path}")
