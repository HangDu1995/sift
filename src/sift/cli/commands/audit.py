"""audit 命令:把 TranscriptAuditor.audit 接到命令行。

对账真实会话 transcript 与 SKILL.md 声明的指令(**不 re-run**,直接对账历史执行),
产出 audit-report.json + console summary(照 init/eval 的 typer 风格)。

设计要点:
- ``TranscriptAuditor`` 在**模块级** import(不像 init/run 把重件 import 放函数内),
  以便测试用 ``monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", ...)``
  替换;模块级名字解析使函数体内的 ``TranscriptAuditor()`` 在调用时取到被替换后的对象。
- 错误处理照 init/run:``SiftError`` → console.print 红色 + ``typer.Exit(code=exc.exit_code)``。
- console summary 着色 pass=green / fail=red / na=yellow;FAIL/N/A 明细用 rich Table
  (PASS 是关注点外的默认态,太多不列;至少列 FAIL/N/A)。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape as rich_markup_escape
from rich.table import Table

from sift.audit.auditor import TranscriptAuditor
from sift.audit.boundary import frontmatter_tool_names
from sift.audit.judge_context import AuditOptions
from sift.audit.suggest import build_suggestions
from sift.cli.main import app, console
from sift.cli.options import options_command
from sift.concurrency import (
    MAX_BATCH_CONCURRENT,
    configure_adaptive,
    configure_max_concurrent,
)
from sift.errors import SiftError
from sift.evals.opencode_db import export_session, list_sessions, resolve_session
from sift.evals.structured import is_structured_records, load_structured_records
from sift.execution.error_triage import classify_error, unresolved_cause_info
from sift.models.audit_models import AuditVerdict
from sift.parser import parse_skill_md, resolve_skill_dir


@dataclass(frozen=True)
class _AuditInputOptions:
    """声明与会话输入的解析选项。"""

    skill_path: Path
    output_dir: Path | None
    kind: str | None
    db: Path | None
    session: str | None
    transcript: list[Path] | None
    locate_turns: bool
    max_concurrent: int | None


@dataclass(frozen=True)
class _AuditEngineOptions:
    """审计引擎选项，与输入文件处理分开。"""

    model: str | None
    refine_instructions: bool
    generate_headline: bool
    locate_turns: bool


@dataclass(frozen=True)
class _AuditOutputOptions:
    """CLI 报告与建议的输出选项。"""

    suggest: bool
    output_format: str
    open_browser: bool


@dataclass(frozen=True)
class _PreparedAuditInput:
    """完成导出、展开和切片后，供审计使用的一组输入。"""

    name: str
    body: str
    transcript: list[Path]
    fm_tools: dict[str, str]
    display_names: list[str] | None
    aux_tool_traces: list | None
    out: Path


@dataclass
class _AuditTemporaryInputs:
    """记录本次命令创建的临时文件，在异常处理后统一清理。"""

    export_tmp: Path | None = None
    expanded_tmps: list[Path] = field(default_factory=list)

    def cleanup(self) -> None:
        """仅清理本次导出和展开产生的临时输入。"""
        if self.export_tmp is not None:
            self.export_tmp.unlink(missing_ok=True)
        for path in self.expanded_tmps:
            path.unlink(missing_ok=True)


_TRUNCATE_WIDTH = 60


def _truncate(text: str, width: int = _TRUNCATE_WIDTH) -> str:
    """单行化 + 截断 + **rich markup 转义**,供表格单元格展示(instruction_text / evidence 可能很长)。

    evidence 是判官 LLM 的输出、instruction_text 可能内嵌 headline(同为 LLM 产物)——
    含 `[/x]` 形态时 rich 按未配对闭合标签抛 MarkupError,直接崩掉整个 audit 命令
    (报告已落盘、exit code 非零,被 CI 误读为审计失败);含真实样式名时文字被错误
    染色。先截断后转义(转义会扩字符,以展示宽度为准)。外层再拼 `[dim](数量)[/dim]`
    一类**有意**的标记不受影响(转义只作用于本函数的输入)。
    """
    flat = text.replace("\n", " ").strip()
    cut = flat if len(flat) <= width else flat[: width - 1] + "…"
    return rich_markup_escape(cut)


def format_unresolved_breakdown(breakdown: dict[str, int]) -> str:
    """UNRESOLVED 根因分布 → 一行字符串(按计数降序);空 → 空串。

    用 unresolved_cause_info 取 (标签, 建议);保留旧"建议重跑"警告,此函数只产追加的分布行。
    让用户从一条笼统警告变成能分辨「该重跑 vs 重跑无益」。纯展示,不进任何判决。
    """
    if not breakdown:
        return ""
    items = sorted(breakdown.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = []
    for cause, n in items:
        label, suggestion = unresolved_cause_info(cause)
        parts.append(f"{n}× {label}({suggestion})")
    return " · ".join(parts)


def _fmt_ms(v: object) -> str:
    """session list 的 updated/created 是 epoch ms,格式化成可读时间;非法/缺省 → '—'。"""
    if not isinstance(v, (int, float)) or not v:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return str(v)


def _audit_from_db(db: Path, session: Optional[str]) -> tuple[Path, str]:
    """``--db`` 解析:list/选 session → export → 写临时 json,返回 (临时 json Path, 显示名)。

    显示名 = ``<session-id>.json``——报告侧(findings.transcript / 终端进度)用它,
    不透出随机临时名(临时 json 用完由 caller unlink)。

    - 单 session 或 ``--session`` 命中 → 导出该 session,返回临时 json Path + 显示名。
    - 多 session 且未指定 → 打 rich 表(id/title/updated)+ exit 2(非交互,让用户带 ``--session`` 重跑)。
    - 0 session / id 不匹配 / opencode 缺失 → ``OpencodeDbError``(被上层 except 兜成 exit 3)。

    ``list_sessions`` / ``export_session`` / ``resolve_session`` 模块级 import,
    便于测试 ``monkeypatch.setattr("sift.cli.commands.audit.list_sessions", …)``。
    """
    sessions = list_sessions(db)
    target = resolve_session(sessions, session)
    if target is None:
        t = Table(title=(f"db 里有 {len(sessions)} 个 session,挑一个重跑(加 --session <id>)"))
        t.add_column("id")
        t.add_column("title")
        t.add_column("updated")
        for s in sessions:
            # title 是模型生成的会话摘要,按不可信文本转义(含 [/x] 会 MarkupError)
            t.add_row(str(s.get("id", "?")), rich_markup_escape(str(s.get("title", ""))), _fmt_ms(s.get("updated")))
        console.print(t)
        console.print(("[red]Error:[/red] 多个 session,请带 --session <id> 重跑(见上表)"))
        raise typer.Exit(code=2)
    sid = target.get("id", "?")
    console.print((f"[dim]--db: 导出 session {sid}({rich_markup_escape(str(target.get('title', '')))})…[/dim]"))
    json_str = export_session(db, sid)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(json_str)
    return Path(f.name), f"{sid}.json"


def _print_scope_hint(transcript: list[Path], name: str) -> None:
    """``--kind`` 未指定时,对称提示 transcript 含哪些可收窄锚点(skill/agent/root 一视同仁)。

    默认(不带 ``--kind``)= 宽松全 session 模式,**三者共同默认**(skill/agent/root 声明都不切分、
    整条 session 对 body 对账)。但 transcript 常含可切的作用域锚点(本 skill 调用 / ``agent==name`` 段 /
    子 agent 作用域)——这些在宽松模式下会混进对账(agent/root 尤甚:角色专属指令扣到不相干 record 上
    → 误 FAIL)。检测到任一锚点就提示可带对应 ``--kind`` 精确收窄。

    **非阻断**:宽松模式是有意选项(尤其 skill 全 session 多落 N/A、误伤轻),提示只让用户知道有精确档,
    不阻止继续。opencode transcript 跳过(单锚点 ``info.agent``,``--kind skill/agent`` 对它无意义)。
    """
    from sift.audit.slice import scope_signals
    from sift.evals.opencode import is_opencode_session

    cc_tps = [tp for tp in transcript if not is_opencode_session(tp)]
    if not cc_tps:
        return
    signals = scope_signals(cc_tps, name)
    if not signals:
        return
    lines = []
    if "skill" in signals:
        lines.append((f"--kind skill   收窄到 {name} skill 活跃段(按 Skill 调用切)"))
    if "agent" in signals:
        lines.append((f"--kind agent   收窄到 agent={name} 段(被 dispatch 的子 agent 执行段)"))
    if "root" in signals:
        lines.append(("--kind root    收窄到主 agent 作用域(排除子 agent 内部)"))
    console.print(
        (
            "[yellow]提示:[/yellow] 未指定 --kind,默认宽松模式(整条 session 对声明 body "
            "对账——skill/agent/root 三者共同默认)。本 transcript "
            "含可收窄锚点,精确审计可加:"
        )
    )
    for ln in lines:
        console.print(f"[yellow]  {ln}[/yellow]")
    console.print(("[yellow](宽松模式是有意选项;加 --kind 走精确档。继续宽松对账…)[/yellow]"))


@dataclass(frozen=True)
class _AuditScope:
    """The declaration scope and turn-rendering choice for transcript slicing."""

    kind: str | None
    name: str
    locate_turns: bool


_AUDIT_SKILL_PATH_HELP = "声明路径:skill 目录(找其下 SKILL.md)、SKILL.md 文件、或任意 agent/workflow .md 文件(直读)"

_AUDIT_TRANSCRIPT_HELP = "真实会话 transcript(cc .jsonl 或 opencode .json;可重复 --transcript a --transcript b)"

_AUDIT_REFINE_INSTRUCTIONS_HELP = (
    "精筛(默认开):LLM 砍掉 extractor 误抽的示例/占位伪指令(audit 提精度,精度>召回);--no-refine-instructions 关闭"
)

_AUDIT_GENERATE_HEADLINE_HELP = (
    "人话标题(默认开):独立 LLM 步骤把每条指令的原文 bullet "
    "去噪+补主语压成一句显示标题(判定不读);--no-headline 关闭省那次 LLM"
)

_AUDIT_LOCATE_TURNS_HELP = (
    "turn 定位(默认关):--turn-refs 时 digest 加 [T{idx}] 前缀、仅 "
    "prohibition/step 类 judge 输出 turn_refs(finding 带违规发生的 turn "
    "序号,供能 link 到 turn 内容的下游消费方;conditional/output 的 FAIL "
    "是段级缺席、无 turn)。cc/structured 切片=全局序号,opencode 切片=段内 "
    "local。默认 --no-turn-refs 回到无前缀的干净判定"
)

_AUDIT_KIND_HELP = (
    "声明单元种类:切本 skill/agent 执行段(skill=按 Skill 调用切;agent=按 agent "
    "归属切;root=切顶层主 agent 作用域(cc parent_tool_use_id 为空 / opencode "
    "session.info.agent;target 名忽略))。不传=审整条 transcript。opencode "
    "transcript 单锚点(info.agent),kind=skill/agent 对它无意义、照常按 agent "
    "字段切"
)

_AUDIT_DB_HELP = (
    "opencode .db 文件:自动 export 单 session 再 audit(需 opencode CLI;不带 "
    "--session 则单 session 自动选、多个列出让你挑了重跑)"
)

_AUDIT_MAX_CONCURRENT_HELP = (
    "LLM 判定并发上限:显式设 = 固定该值(关自适应);不设 = 默认 AIMD "
    "自适应(动态探当前后端容量边界,通用工具适配多后端)。也读 env "
    "SIFT_MAX_CONCURRENT;env SIFT_ADAPTIVE_CONCURRENCY=0 显式关自适应"
)

_AUDIT_SUGGEST_HELP = (
    "改进建议(默认关,审计默认姿态是纯判决):对 FAIL findings 做三类归因的 LLM "
    "后处理——改 skill 建议 / 改声明建议 / "
    "归因模型·环境的只透明计数不出建议。仅供参考,不进任何 gate、不影响 exit "
    "code;全 PASS 不跑不落盘"
)


_AUDIT_SKILL_PATH_ARGUMENT = typer.Argument(
    ...,
    help=(_AUDIT_SKILL_PATH_HELP),
)

_AUDIT_TRANSCRIPT_OPTION = typer.Option(
    None,
    "--transcript",
    help=(_AUDIT_TRANSCRIPT_HELP),
)

_AUDIT_OUTPUT_DIR_OPTION = typer.Option(None, "--output", "-o", help="输出目录(默认 ./output)")

_AUDIT_MODEL_OPTION = typer.Option(None, "--model", help="语义判定 LLM")

_AUDIT_REFINE_INSTRUCTIONS_OPTION = typer.Option(
    True,
    ("--refine-instructions/--no-refine-instructions"),
    help=(_AUDIT_REFINE_INSTRUCTIONS_HELP),
)

_AUDIT_GENERATE_HEADLINE_OPTION = typer.Option(
    True,
    "--headline/--no-headline",
    help=(_AUDIT_GENERATE_HEADLINE_HELP),
)

_AUDIT_LOCATE_TURNS_OPTION = typer.Option(
    False,
    "--turn-refs/--no-turn-refs",
    help=(_AUDIT_LOCATE_TURNS_HELP),
)

_AUDIT_KIND_OPTION = typer.Option(
    None,
    "--kind",
    help=(_AUDIT_KIND_HELP),
)

_AUDIT_OUTPUT_FORMAT_OPTION = typer.Option("table", "--format", help="table|json")

_AUDIT_OPEN_BROWSER_OPTION = typer.Option(False, "--open", help="生成后用浏览器打开 audit-report.html")

_AUDIT_DB_OPTION = typer.Option(
    None,
    "--db",
    help=(_AUDIT_DB_HELP),
)

_AUDIT_SESSION_OPTION = typer.Option(
    None,
    "--session",
    help=("--db 下指定 session id;不给则单 session 自动选、多个列出让你挑了重跑"),
)

_AUDIT_MAX_CONCURRENT_OPTION = typer.Option(
    None,
    "--max-concurrent",
    help=(_AUDIT_MAX_CONCURRENT_HELP),
)

_AUDIT_SUGGEST_OPTION = typer.Option(
    False,
    "--suggest/--no-suggest",
    help=(_AUDIT_SUGGEST_HELP),
)


@dataclass
class AuditCommandOptions:
    """Command option declarations; parsed with Typer before execution."""

    skill_path: Path = _AUDIT_SKILL_PATH_ARGUMENT
    transcript: Optional[list[Path]] = _AUDIT_TRANSCRIPT_OPTION
    output_dir: Optional[Path] = _AUDIT_OUTPUT_DIR_OPTION
    model: Optional[str] = _AUDIT_MODEL_OPTION
    refine_instructions: bool = _AUDIT_REFINE_INSTRUCTIONS_OPTION
    generate_headline: bool = _AUDIT_GENERATE_HEADLINE_OPTION
    locate_turns: bool = _AUDIT_LOCATE_TURNS_OPTION
    kind: Optional[str] = _AUDIT_KIND_OPTION
    output_format: str = _AUDIT_OUTPUT_FORMAT_OPTION
    open_browser: bool = _AUDIT_OPEN_BROWSER_OPTION
    db: Optional[Path] = _AUDIT_DB_OPTION
    session: Optional[str] = _AUDIT_SESSION_OPTION
    max_concurrent: Optional[int] = _AUDIT_MAX_CONCURRENT_OPTION
    suggest: bool = _AUDIT_SUGGEST_OPTION


@options_command(app, AuditCommandOptions, rich_help_panel="审计")
def audit(cli_options: AuditCommandOptions):
    """对账真实 session transcript 与声明(不 re-run,直接审历史执行)。"""
    _run_audit_command(
        _AuditInputOptions(
            cli_options.skill_path,
            cli_options.output_dir,
            cli_options.kind,
            cli_options.db,
            cli_options.session,
            cli_options.transcript,
            cli_options.locate_turns,
            cli_options.max_concurrent,
        ),
        _AuditEngineOptions(
            cli_options.model, cli_options.refine_instructions, cli_options.generate_headline, cli_options.locate_turns
        ),
        _AuditOutputOptions(cli_options.suggest, cli_options.output_format, cli_options.open_browser),
    )


def _print_finding_details(report, src_of):
    non_pass = [f for f in report.findings if f.verdict != AuditVerdict.PASS]
    if non_pass:
        table = Table(title=("Findings (FAIL / UNRESOLVED / INDETERMINATE / N/A)"))
        table.add_column("verdict", style="bold")
        table.add_column("category")
        table.add_column("instruction_text")
        table.add_column("transcript")
        table.add_column("evidence")
        for f in non_pass:
            if f.verdict == AuditVerdict.FAIL:
                color = "red"
            elif f.verdict in (AuditVerdict.UNRESOLVED, AuditVerdict.INDETERMINATE):
                color = "yellow"
            else:
                color = "dim"
            tcol = f.transcript
            if src_of.get(f.transcript):
                tcol = f"{f.transcript} [dim]· {src_of[f.transcript]}[/dim]"
            # 数量 finding 沿用原指令原文(对聚合透明),不加记号报告里认不出;显式 (数量)
            # 前缀对齐 ORDER 合成 finding 自带的 (顺序) 文案。
            icol = _truncate(f.instruction_text)
            if f.control_flow == "quantity":
                icol = f"[dim](数量)[/dim] {icol}"
            table.add_row(
                f"[{color}]{f.verdict.value}[/{color}]",
                f.category.value,
                icol,
                tcol,
                _truncate(f.evidence),
            )
        console.print(table)


def _print_instruction_tables(report):
    """Display failures and inapplicable instructions in separate tables."""
    if len(report.transcripts) > 1 and report.by_instruction:
        n = len(report.transcripts)

        fail_rows = [r for r in report.by_instruction if r.fail_count > 0]
        if fail_rows:  # 已按 fail_rate 降序
            t = Table(title=(f"By instruction — 违规 FAIL (across {n} transcripts)"))
            t.add_column("明细 #", style="dim")
            t.add_column("fail%", style="bold")
            t.add_column("instruction")
            t.add_column("category")
            t.add_column("fail/total")
            t.add_column("fail in transcripts")
            for row in fail_rows:
                color = "red" if row.fail_rate >= 0.5 else "yellow"
                t.add_row(
                    ", ".join(f"#{s}" for s in row.fail_seqs),
                    (f"[{color}]{row.fail_rate * 100:.0f}%[/{color}]"),
                    _truncate(row.instruction_text),
                    row.category.value,
                    f"{row.fail_count}/{row.total}",
                    ", ".join(row.fail_in_transcripts) if row.fail_in_transcripts else "—",
                )
            console.print(t)

        na_rows = [r for r in report.by_instruction if r.na_count > 0]
        if na_rows:
            na_rows.sort(key=lambda r: (-r.na_count, r.instruction_text))  # 不适用的段多的在前
            t = Table(title=(f"By instruction — 场景不适用 N/A (across {n} transcripts)"))
            t.add_column("明细 #", style="dim")
            t.add_column("na/total", style="bold")
            t.add_column("instruction")
            t.add_column("category")
            t.add_column("na in transcripts")
            for row in na_rows:
                t.add_row(
                    ", ".join(f"#{s}" for s in row.na_seqs),
                    f"{row.na_count}/{row.total}",
                    _truncate(row.instruction_text),
                    row.category.value,
                    ", ".join(row.na_in_transcripts),
                )
            console.print(t)


def _print_audit_usage(report, sug):
    """Display engine and suggestion usage without losing either contribution."""
    _sug_ran = sug is not None and sug.calls > 0
    tu = report.token_usage
    if tu.get("calls"):
        cache_read = tu.get("cache_read_input_tokens", 0)
        console.print(
            (f"[dim]LLM tokens:[/dim] input={tu.get('input_tokens', 0)} output={tu.get('output_tokens', 0)}")
            + (f" cache_read={cache_read}" if cache_read else "")
            + f" [dim]· {tu['calls']} call(s)[/dim]"  # 仅展示 token；HTTP 路径未记费用
            + (
                (f" [dim]+ 建议 input={sug.input_tokens} output={sug.output_tokens} · {sug.calls} call(s)[/dim]")
                if _sug_ran
                else ""
            )
        )
    elif _sug_ran:
        # 引擎零 LLM 调用(refine/headline 全缓存命中 + 判定全程序对账)而 --suggest
        # 真跑了:token 汇总只剩建议段,也要报——不能随引擎 calls 门一起被吞掉
        # (耗时行同场景是并计的,两行口径须一致)。
        console.print(
            (
                f"[dim]LLM tokens:[/dim] 建议 input={sug.input_tokens} "
                f"output={sug.output_tokens} [dim]· {sug.calls} call(s)[/dim]"
            )
        )
    if report.duration_seconds:
        _eng_s = round(report.duration_seconds)
        console.print(
            f"[dim]耗时:[/dim] {_eng_s}s"
            + (
                (f" [dim]+ 建议生成 {_sug_s}s = {_eng_s + _sug_s}s[/dim]")
                if _sug_ran and (_sug_s := round(sug.duration_seconds))
                else ""
            )
        )


def _print_audit_coverage(report):
    """Display declaration coverage, tool boundaries, and provenance availability."""
    cov = report.coverage
    if cov.total:
        console.print(
            (
                f"声明覆盖: [bold]{cov.covered}/{cov.total}[/bold] "
                f"({cov.coverage_rate * 100:.0f}%)  "
                f"[dim]未覆盖 {cov.uncovered} · 未知 {cov.unknown}[/dim]"
            )
        )

    # 工具对账一行(越界视图一期,信息性):轨迹里的领域工具动作 × 声明工具要求。
    # 未声明 ≠ 违规(声明通常不穷举工具),非零才打,只提示人去看 HTML 的 boundary 区块。
    bnd = report.boundary
    if bnd.undeclared:
        n_bnd_sessions = _count_boundary_sessions(bnd.tools)
        console.print(
            (
                f"工具对账: 未声明领域工具 [bold]{bnd.undeclared}[/bold] 个 · "
                f"{n_bnd_sessions} 会话"
                f"[dim](不构成违规判定;详见报告 boundary 区块)[/dim]"
            )
        )

    # 文件归因视图(v2):Analyzer 结果独立于声明遵循性 verdict；仅在存在可用
    # provenance 证据时显示一行摘要，完整 findings/timeline 随 JSON 嵌入报告。
    if report.provenance:
        capture_count = sum(result.get_count("capture_count") for result in report.provenance.values())
        effect_count = sum(result.get_count("effect_count") for result in report.provenance.values())
        console.print(
            (
                f"文件归因: {len(report.provenance)} 个会话 · "
                f"{capture_count} 个 capture · {effect_count} 个文件变更"
                f"[dim](不参与声明 PASS/FAIL；详见报告 provenance 区块)[/dim]"
            )
        )


def _configure_audit_concurrency(max_concurrent):
    """Validate explicit concurrency before selecting fixed or adaptive limits."""
    mc = max_concurrent
    if mc is None:
        env_mc = os.environ.get("SIFT_MAX_CONCURRENT")
        if env_mc:  # None 或空串都视为未设
            try:
                mc = int(env_mc)
            except ValueError as _caught_error:
                console.print((f"[red]Error:[/red] SIFT_MAX_CONCURRENT 非整数: {env_mc!r}"))
                raise typer.Exit(code=2) from _caught_error
    if mc is not None:
        if mc < 1:
            console.print((f"[red]Error:[/red] --max-concurrent 必须 ≥ 1,收到 {mc}"))
            raise typer.Exit(code=2)
        configure_max_concurrent(mc)
    elif os.environ.get("SIFT_ADAPTIVE_CONCURRENCY", "1") == "0":
        configure_max_concurrent(MAX_BATCH_CONCURRENT)
    else:
        configure_adaptive()


def _expand_structured_transcripts(transcript, display_names, expanded_tmps):
    """Expand structured input while tracking temporary files for caller cleanup."""
    if any(is_structured_records(tp) for tp in transcript):
        display_names = [tp.name for tp in transcript]  # 先记原始名(同序),再替换成临时文件
        expanded: list[Path] = []
        for tp in transcript:
            if not is_structured_records(tp):
                expanded.append(tp)
                continue
            recs = load_structured_records(tp)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs))
            expanded_tmps.append(Path(f.name))
            expanded.append(Path(f.name))
        transcript = expanded
    return transcript, display_names


def _slice_audit_transcripts(scope: _AuditScope, transcript, out: Path, display_names):
    """Slice selected scopes and retain stable input names and auxiliary traces."""
    kind, name, locate_turns = scope.kind, scope.name, scope.locate_turns
    aux_tool_traces: list[list] | None = None  # A1 补充证据通道(kind=root 才填充,见下)
    if kind:
        # 先切所有段(暂存内存):确认有段再建目录,never-invoked → exit 2 不留空 slices/。
        # seg = (tp, ti, i, payload, is_oc);payload:cc=record 列表,opencode=子 session dict。
        segs = _collect_audit_segments(transcript, kind, name)
        if not segs:
            # root 时 name(declared skill 名)对 root 无意义(root 按 parent_tool_use_id /
            # session.info.agent 切,不按 name 契约)→ 不提 declared name。注:cc root 主作用域
            # 在非空 transcript 下恒非空、opencode 缺 info.agent 已走前面 exit 2,故 root 时
            # 此分支 latent in 非空 transcript(完全空 transcript 仍触发:无 record → 主作用域
            # 也为空);防御性文案,不引入行为变化。
            who = f"'{name}'" if kind != "root" else "主 agent 作用域"
            console.print((f"[red]Error:[/red] {who} 未在此 transcript 中以 --kind {kind} 锚点出现(无可对账段)"))
            raise typer.Exit(code=2)
        slice_dir = out / "slices"
        slice_dir.mkdir(parents=True, exist_ok=True)
        sliced: list[Path] = []
        # A1 补充证据通道(kind=root 专属):被 root 切片排除的子代理全量工具痕迹,
        # 与 sliced 同序(每个 seg 一份;cc/structured 按 parent_tool_use_id,
        # opencode 按非主 agent message——build_subagent_tool_trace 内部按格式分派)。
        # 条件类条目 root trace 未命中时在子代理痕迹里做第二遍确定性匹配(auditor),
        # 洗"执行类动作全在子代理、主 scope 恒缺席"的缺席误判。
        # env SIFT_AUX_TRACE=0 显式关闭(A/B 实验与副作用逃生口;默认开)。
        if kind == "root" and os.environ.get("SIFT_AUX_TRACE", "1") != "0":
            aux_tool_traces = []
        _aux_by_tp: dict[Path, list] = {}  # 同一 tp 多段只读一次文件
        for tp, ti, i, seg, is_oc in segs:
            _append_auxiliary_trace(aux_tool_traces, tp, _aux_by_tp, locate_turns)
            ext = ".json" if is_oc else ".jsonl"
            # 切片文件名用**原始输入名**的 stem(结构化/--db 展开的临时 stem 不进
            # 文件名——切片文件持久化在 out/slices/,其名即 findings.transcript)。
            stem_src = display_names[ti - 1] if display_names is not None and ti - 1 < len(display_names) else tp.name
            sp = slice_dir / (f"{Path(stem_src).stem}-{name}-{ti}-{i}{ext}")
            if is_oc:
                sp.write_text(
                    json.dumps(seg, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                sp.write_text(
                    "\n".join(json.dumps(r, ensure_ascii=False) for r in seg),
                    encoding="utf-8",
                )
            sliced.append(sp)
        transcript = sliced
        # 切片文件名已含原始输入 stem,本身就是稳定、可回溯的显示名——引擎侧
        # 直接用切片文件名,不再叠一层 display_names(长度也与切片数对不上)。
        display_names = None
    return transcript, display_names, aux_tool_traces


def _append_auxiliary_trace(aux_tool_traces, tp, _aux_by_tp, locate_turns):
    from sift.audit.slice import build_subagent_tool_trace

    if aux_tool_traces is not None:
        # locate_turns 透传:与主 trace 同体系——报告关闭 [T{idx}] 渲染时
        # aux 命中的 evidence 也不带 (T{n}) 引用,免得出现无锚点可对的序号
        # (opencode 形本就不带 turn,见其构建器 docstring)。
        if tp not in _aux_by_tp:
            _aux_by_tp[tp] = build_subagent_tool_trace(tp, locate_turns=locate_turns)
        aux_tool_traces.append(_aux_by_tp[tp])


def _collect_audit_segments(transcript, kind, name):
    from sift.audit.slice import slice_transcript
    from sift.evals.opencode import (
        is_opencode_session,
        slice_opencode_by_agent,
    )

    segs: list[tuple[Path, int, int, object, bool]] = []
    for ti, tp in enumerate(transcript, 1):
        if is_opencode_session(tp):
            # root:target 取 session.info.agent(opencode 无父子作用域,root 即主 agent);
            # skill/agent:照旧按声明名(name)切。
            target_agent = _opencode_scope_agent(kind, tp, name)
            for i, seg in enumerate(slice_opencode_by_agent(tp, target_agent), 1):
                segs.append((tp, ti, i, seg, True))
        else:
            for i, seg in enumerate(slice_transcript(tp, name, kind), 1):
                segs.append((tp, ti, i, seg, False))
    return segs


def _opencode_scope_agent(kind, tp, name):
    from sift.evals.opencode import load_opencode_session

    if kind == "root":
        sess = load_opencode_session(tp)
        target_agent = (sess.get("info") or {}).get("agent")
        if not target_agent:
            console.print(("[red]Error:[/red] --kind root:opencode session 缺 info.agent,无法确定主 agent"))
            raise typer.Exit(code=2)
    else:
        target_agent = name
    return target_agent


def _validate_audit_inputs(db, session, transcript):
    """Reject conflicting or missing audit input options."""
    if db is not None and transcript:
        console.print(("[red]Error:[/red] --db 与 --transcript 互斥,二选一"))
        raise typer.Exit(code=2)
    if session is not None and db is None:
        console.print("[red]Error:[/red] --session 仅与 --db 搭配使用")
        raise typer.Exit(code=2)
    if db is None and not transcript:
        console.print(("[red]Error:[/red] 需要 --transcript 或 --db 之一"))
        raise typer.Exit(code=2)


def _count_boundary_sessions(tools) -> int:
    """Count distinct sessions containing undeclared domain tools."""
    sessions: set[str] = set()
    for tool in tools:
        if tool.classification == "undeclared-domain":
            sessions.update(tool.transcripts)
    return len(sessions)


def _attach_audit_suggestions(report, body: str, model: str | None) -> None:
    """将改进建议及真实消耗加入报告，保留未调用模型的跳过结果。"""
    suggest_result = build_suggestions(
        report,
        body,
        model=model,
        on_progress=lambda msg: console.print(msg),  # 阶段头/批级行与判官进度同明文风格
    )
    if suggest_result is not None:
        report.suggestions = suggest_result
        # 消耗并入报告阶段表(suggest 行):suggest 是 CLI 层后处理,引擎墙钟
        # (duration_seconds)不含它——不单列一行,「时间花哪了」就看不见这段。
        # 只计真跑过 LLM 的(calls>0);UNRESOLVED 过高被拦 / 无 FAIL 不产 →
        # 零消耗不占行。token/calls 与判官阶段同口径(含截断/升档重试)。
        if suggest_result.calls > 0:
            report.stage_breakdown.append(
                {
                    "stage": "suggest",
                    "seconds": round(suggest_result.duration_seconds, 3),
                    "input_tokens": suggest_result.input_tokens,
                    "output_tokens": suggest_result.output_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "calls": suggest_result.calls,
                    "note": "",
                }
            )


def _write_audit_reports(report, out: Path) -> tuple[Path, Path]:
    """写出相同数据的 JSON 和 HTML 报告，返回实际输出路径。"""
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "audit-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # HTML 报告(与 JSON 并列,always 写;对齐路径3 scoring report 视觉)
    from sift.reporting.audit_viewer import AuditReportGenerator

    html_path = out / "audit-report.html"
    AuditReportGenerator().write_html(report, html_path)

    return report_path, html_path


def _print_audit_report(report, name: str, report_path: Path, html_path: Path) -> None:
    """打印审计摘要、建议、覆盖和明细。"""
    src_of = dict(zip(report.transcripts, report.transcript_sources))
    summary = report.summary
    total = summary.get("total", 0)
    n_pass = summary.get("pass", 0)
    n_fail = summary.get("fail", 0)
    n_na = summary.get("na", 0)

    src_kinds = sorted(set(report.transcript_sources))
    src_tail = (f"  [dim]sources: {' · '.join(src_kinds)}[/dim]") if src_kinds else ""
    console.print(
        (
            f"Audited {len(report.transcripts)} transcript(s) for [bold]{name}[/bold]: "
            f"total={total} "
            f"pass=[green]{n_pass}[/green] "
            f"fail=[red]{n_fail}[/red] "
            f"na=[yellow]{n_na}[/yellow]{src_tail}"
        )
    )

    # 声明覆盖一行(covered=任一 transcript PASS/FAIL,uncovered=全 N/A,unknown=说不清;
    # 反推指令不计):本批对账的证据面铺了声明的多大比例——低覆盖时上面那行 fail 数只
    # 代表被触达的子集。total=0(空声明/全 derived)不显。
    _print_audit_coverage(report)

    # 改进建议一行(--suggest 产;渲染优先级 skipped_reason 先行——失败 ≠ 全怪模型)。
    _print_audit_suggestion_summary(report)
    sug = report.suggestions
    _print_audit_usage(report, sug)

    if report.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in report.warnings:
            console.print(f"  [yellow]- {w}[/yellow]")

    breakdown_line = format_unresolved_breakdown(report.unresolved_breakdown)
    if breakdown_line:
        console.print((f"[yellow]UNRESOLVED 根因:[/yellow] {breakdown_line}"))

    # 多 transcript:展示指令级聚合。fail 与 N/A(场景不适用)语义不同 → 分两张表:
    #   违规表看 fail%/fail/total(系统性违规,如"NEVER fabricate 在 75% 会话被违反");
    #   场景不适用表看 na/total + 哪几段不适用。
    # 不再混在一张用 fail 列 —— 那会让 N/A 行显示 0% fail / fail/total 0/2,看着像有
    # fail、误导(N/A 不是 fail)。一条指令既有 fail 又有 na(某段违反、某段不适用)→ 两表都出现,
    # 各反映一面。全 PASS 仍隐藏(默认正常态,太多)。单 transcript 不显示(聚合无额外洞察)。
    _print_instruction_tables(report)

    # 只列非 PASS 明细(PASS 是默认正常态,太多;FAIL/UNRESOLVED/INDETERMINATE 才是关注点,N/A 不适用也列)。
    _print_finding_details(report, src_of)

    console.print(f"Written to {report_path}")
    console.print(f"Report written to {html_path}")


def _print_audit_suggestion_summary(report) -> None:
    """打印建议分类、跳过原因和模型输出丢弃数量。"""
    sug = report.suggestions
    if sug is not None:
        if sug.skipped_reason:
            # skipped_reason 含异常文本(HTTP 响应体片段可拼进来)——转义防 markup 注入
            console.print((f"[yellow]改进建议未生成:[/yellow] {rich_markup_escape(sug.skipped_reason)}"))
        else:
            n_skill = sum(1 for s in sug.suggestions if s.category == "skill_fix")
            n_decl = sum(1 for s in sug.suggestions if s.category == "declaration_fix")
            if sug.suggestions or sug.model_env_count:
                console.print(
                    (
                        f"改进建议: skill_fix [bold]{n_skill}[/bold] 项 · "
                        f"declaration_fix [bold]{n_decl}[/bold] 项 · "
                        f"另有 {sug.model_env_count} 条 FAIL 归因模型/环境不出建议"
                        f"[dim](--suggest 产,仅供参考;详见报告建议区块)[/dim]"
                    )
                )
            if sug.dropped_count:
                console.print((f"[dim]建议生成: {sug.dropped_count} 条模型输出不合规已丢弃[/dim]"))

    # 底部汇总是「这次执行总消耗」:--suggest 跑过时(calls>0)建议段在引擎墙钟与
    # token_usage 之外,须并计——否则上面建议行说 133s、底下汇总却只报判定 19s,自相矛盾。


def _prepare_audit_input(options: _AuditInputOptions, temporary: _AuditTemporaryInputs) -> _PreparedAuditInput:
    """读取声明并准备会话输入，临时文件由调用方按原顺序清理。"""
    skill_path = options.skill_path
    output_dir = options.output_dir
    kind = options.kind
    db = options.db
    session = options.session
    transcript = options.transcript
    locate_turns = options.locate_turns
    max_concurrent = options.max_concurrent
    _configure_audit_concurrency(max_concurrent)

    name, _desc, _content, _frontmatter, body = parse_skill_md(skill_path)
    # frontmatter 工具白名单(agent 声明的 tools: / skill 的 allowed-tools:)喂越界
    # 视图对账(第四声明源;抽不出 → 空 dict,引擎侧零影响)。
    fm_tools = frontmatter_tool_names(_frontmatter)

    if kind is not None and kind not in ("skill", "agent", "root"):
        console.print((f"[red]Error:[/red] --kind 只接受 skill / agent / root,收到 {kind!r}"))
        raise typer.Exit(code=2)

    out = output_dir or Path("output")

    # 输入源:--db 与 --transcript 互斥;--session 仅搭 --db;都不给 → exit 2。
    _validate_audit_inputs(db, session, transcript)

    transcript = transcript or []
    # 显示名(与 transcript 同序):--db 导出 / 结构化输入展开都会产生**临时文件**,
    # 报告侧(findings.transcript / provenance 键 / 终端进度)一律用原始输入名——
    # 临时名跑一次变一次、跑完即删,写进 audit-report 下游对不上账(同一次证据包
    # 跑两遍也无法 diff)。None = 无临时替换,引擎侧直接用 tp.name(行为与旧版一致)。
    display_names: list[str] | None = None
    if db is not None:
        # --db 解析:list/选 session → export → 临时 json;多 session 在此 exit 2。
        # 解析后 transcript = [临时 json],后续 --kind/引擎照常走(opencode .json 经
        # rebuild 的 is_opencode_session 分派 → build_digest_from_opencode)。
        temporary.export_tmp, export_name = _audit_from_db(db, session)
        transcript = [temporary.export_tmp]
        display_names = [export_name]

    # 结构化 records 输入(--transcript 指向 {format:"sift-records"} 的 JSON):
    # 展开成临时 cc-jsonl 替进去,后续 --kind 切分 / rebuild 全按 cc 路径走,零改动。
    # producer(insight 等)因此不必手拼 cc-jsonl(免 tool_use/tool_result 拆分、Skill 大小写)。
    transcript, display_names = _expand_structured_transcripts(transcript, display_names, temporary.expanded_tmps)

    # --kind 未指定 → 宽松全 session 模式(三者共同默认)。transcript 含可收窄锚点
    # (本 skill 调用 / agent==name / 子 agent 作用域)时,对称提示可带 --kind 精确收窄
    # (非阻断:宽松是有意选项)。检测放 opencode 之外(slice 单扫 cc/structured)。
    if kind is None:
        _print_scope_hint(transcript, name)

    # --kind:切本声明单元被调用/被 dispatch 期间的段,每段写一个子段文件
    # (.jsonl=cc / .json=opencode),再喂 TranscriptAuditor(已支持多 transcript 聚合)。
    # 分派:transcript **格式**选切片器——opencode(单锚点 info.agent,无 Skill tool_use)
    # → slice_opencode_by_agent(kind=skill/agent 按声明名切;kind=root 取 session.info.agent);
    #   cc/structured(三锚点)→ slice_transcript(kind 选 root/skill/agent 锚点,不并集)。
    # 文件名带 transcript 级序号 ti:多个 --transcript 可能同名 stem,仅靠内层 i 会碰撞 → 后者覆盖前者。
    transcript, display_names, aux_tool_traces = _slice_audit_transcripts(
        _AuditScope(kind, name, locate_turns), transcript, out, display_names
    )

    # on_progress:audit 每批 LLM 判定 40-257s,边跑边报进度(transcript 头 + 每批 [k/总] + 耗时),
    # 否则全程静默、用户不知是否在跑。console.print 即时 flush,长跑也实时可见。
    # aux_tool_traces 只在 kind=root 切片路径填充;None 时不传 kwarg——对
    # TranscriptAuditor 的调用形态与旧版完全一致(测试 fake / 外部调用方零感知)。
    # display_names 同理:只在「临时文件直接喂引擎」(无 --kind 的展开/--db)时传。
    return _PreparedAuditInput(name, body, transcript, fm_tools, display_names, aux_tool_traces, out)


def _execute_audit(inputs: _PreparedAuditInput, options: _AuditEngineOptions):
    """调用审计引擎，只在存在补充证据通道证据或显示名时传入对应选项。"""
    return TranscriptAuditor().audit(
        skill_name=inputs.name,
        skill_body=inputs.body,
        transcripts=inputs.transcript,
        options=AuditOptions(
            model=options.model,
            refine_instructions=options.refine_instructions,
            generate_headline=options.generate_headline,
            locate_turns=options.locate_turns,
            declared_tools=inputs.fm_tools,
            on_progress=lambda msg: console.print(msg),
            aux_tool_traces=inputs.aux_tool_traces,
            display_names=inputs.display_names,
        ),
    )


def _run_audit_command(
    input_options: _AuditInputOptions, engine_options: _AuditEngineOptions, output_options: _AuditOutputOptions
) -> None:
    """依次准备输入、审计和输出，保留错误提示及临时文件清理顺序。"""
    input_options = replace(input_options, skill_path=resolve_skill_dir(input_options.skill_path))
    temporary = _AuditTemporaryInputs()
    try:
        prepared = _prepare_audit_input(input_options, temporary)
        report = _execute_audit(prepared, engine_options)
    except SiftError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        triage = classify_error(str(exc))
        console.print((f"[yellow]根因:[/yellow] {triage.root_cause} — {triage.suggestion}"))
        raise typer.Exit(code=exc.exit_code) from exc
    finally:
        temporary.cleanup()

    name, body, out = prepared.name, prepared.body, prepared.out
    model = engine_options.model
    suggest = output_options.suggest
    output_format = output_options.output_format
    open_browser = output_options.open_browser

    # 改进建议(--suggest 可选,CLI 层后处理——引擎零感知,run 内 compliance 不经这里)。
    # build_suggestions 永不外抛:无 FAIL → None(正常终态不落盘);残缺/失败 → 空结果 +
    # skipped_reason(异常态留痕)。token 记在结果自身,不进 report.token_usage(扁平数值键)。
    if suggest:
        _attach_audit_suggestions(report, body, model)

    report_path, html_path = _write_audit_reports(report, out)

    if output_format == "json":
        console.print(report.model_dump_json(indent=2))
        console.print(f"Written to {report_path}")
        return

    _print_audit_report(report, name, report_path, html_path)

    if open_browser:
        import webbrowser

        webbrowser.open(f"file://{html_path.resolve()}")
