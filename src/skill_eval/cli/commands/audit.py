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

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from skill_eval.audit.auditor import TranscriptAuditor, configure_max_concurrent
from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.evals.opencode_db import export_session, list_sessions, resolve_session
from skill_eval.evals.structured import is_structured_records, load_structured_records
from skill_eval.models.audit_models import AuditVerdict
from skill_eval.parser import parse_skill_md, resolve_skill_dir

_TRUNCATE_WIDTH = 60


def _truncate(text: str, width: int = _TRUNCATE_WIDTH) -> str:
    """单行化 + 截断,供表格单元格展示(instruction_text / evidence 可能很长)。"""
    flat = text.replace("\n", " ").strip()
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _fmt_ms(v: object) -> str:
    """session list 的 updated/created 是 epoch ms,格式化成可读时间;非法/缺省 → '—'。"""
    if not isinstance(v, (int, float)) or not v:
        return "—"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return str(v)


def _audit_from_db(db: Path, session: Optional[str]) -> Path:
    """``--db`` 解析:list/选 session → export → 写临时 json,返回其 Path(用完由 caller unlink)。

    - 单 session 或 ``--session`` 命中 → 导出该 session,返回临时 json Path。
    - 多 session 且未指定 → 打 rich 表(id/title/updated)+ exit 2(非交互,让用户带 ``--session`` 重跑)。
    - 0 session / id 不匹配 / opencode 缺失 → ``OpencodeDbError``(被上层 except 兜成 exit 3)。

    ``list_sessions`` / ``export_session`` / ``resolve_session`` 模块级 import,
    便于测试 ``monkeypatch.setattr("skill_eval.cli.commands.audit.list_sessions", …)``。
    """
    sessions = list_sessions(db)
    target = resolve_session(sessions, session)
    if target is None:
        t = Table(title=f"db 里有 {len(sessions)} 个 session,挑一个重跑(加 --session <id>)")
        t.add_column("id")
        t.add_column("title")
        t.add_column("updated")
        for s in sessions:
            t.add_row(str(s.get("id", "?")), str(s.get("title", "")), _fmt_ms(s.get("updated")))
        console.print(t)
        console.print("[red]Error:[/red] 多个 session,请带 --session <id> 重跑(见上表)")
        raise typer.Exit(code=2)
    sid = target.get("id", "?")
    console.print(f"[dim]--db: 导出 session {sid}({target.get('title', '')})…[/dim]")
    json_str = export_session(db, sid)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        f.write(json_str)
    return Path(f.name)


@app.command()
def audit(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    transcript: Optional[list[Path]] = typer.Option(
        None,
        "--transcript",
        help="真实会话 transcript(cc .jsonl 或 opencode .json;可重复 --transcript a --transcript b)",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output", "-o", help="输出目录(默认 ./output)"
    ),
    model: Optional[str] = typer.Option(None, "--model", help="语义判定 LLM"),
    refine_instructions: bool = typer.Option(True, "--refine-instructions/--no-refine-instructions", help="精筛(默认开):LLM 砍掉 extractor 误抽的示例/占位伪指令(audit 提精度,精度>召回);--no-refine-instructions 关闭"),
    locate_turns: bool = typer.Option(
        False, "--turn-refs/--no-turn-refs",
        help="turn 定位(默认关):--turn-refs 时 digest 加 [T{idx}] 前缀、仅 prohibition/step 类 judge "
             "输出 turn_refs(finding 带违规发生的 turn 序号,供能 link 到 turn 内容的下游消费方;"
             "conditional/output 的 FAIL 是段级缺席、无 turn)。cc/structured 切片=全局序号,opencode "
             "切片=段内 local。默认 --no-turn-refs 回到无前缀的干净判定",
    ),
    kind: Optional[str] = typer.Option(
        None,
        "--kind",
        help=("声明单元种类:切本 skill/agent 执行段(skill=按 Skill 调用切;agent=按 agent 归属切;"
              "root=切顶层主 agent 作用域(cc parent_tool_use_id 为空 / opencode session.info.agent;"
              "target 名忽略))。不传=审整条 transcript。opencode transcript 单锚点(info.agent),"
              "kind=skill/agent 对它无意义、照常按 agent 字段切"),
    ),
    format: str = typer.Option("table", "--format", help="table|json"),
    open_browser: bool = typer.Option(
        False, "--open", help="生成后用浏览器打开 audit-report.html"
    ),
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="opencode .db 文件:自动 export 单 session 再 audit(需 opencode CLI;不带 --session 则单 session 自动选、多个列出让你挑了重跑)",
    ),
    session: Optional[str] = typer.Option(
        None,
        "--session",
        help="--db 下指定 session id;不给则单 session 自动选、多个列出让你挑了重跑",
    ),
    max_concurrent: Optional[int] = typer.Option(
        None,
        "--max-concurrent",
        help=("LLM 判定并发上限(默认 6;大声明/低内存/多 audit 共存时调小,如 3,用时间换内存)。"
              "也读 env SKILL_EVAL_MAX_CONCURRENT;CLI 显式值优先"),
    ),
):
    """Audit real session transcript(s) against SKILL.md(不 re-run,直接对账真实历史执行)。"""
    skill_path = resolve_skill_dir(skill_path)
    export_tmp: Optional[Path] = None  # --db 导出的临时 json,finally 清理
    expanded_tmps: list[Path] = []     # 结构化输入展开出的临时 cc-jsonl,finally 清理
    try:
        # 并发上限:CLI --max-concurrent > env SKILL_EVAL_MAX_CONCURRENT > 默认 6(不调 configure)
        mc = max_concurrent
        if mc is None:
            env_mc = os.environ.get("SKILL_EVAL_MAX_CONCURRENT")
            if env_mc:  # None 或空串都视为未设
                try:
                    mc = int(env_mc)
                except ValueError:
                    console.print(f"[red]Error:[/red] SKILL_EVAL_MAX_CONCURRENT 非整数: {env_mc!r}")
                    raise typer.Exit(code=2)
        if mc is not None:
            if mc < 1:
                console.print(f"[red]Error:[/red] --max-concurrent 必须 ≥ 1,收到 {mc}")
                raise typer.Exit(code=2)
            configure_max_concurrent(mc)

        name, _desc, _content, _frontmatter, body = parse_skill_md(skill_path)

        if kind is not None and kind not in ("skill", "agent", "root"):
            console.print(f"[red]Error:[/red] --kind 只接受 skill / agent / root,收到 {kind!r}")
            raise typer.Exit(code=2)

        out = output_dir or Path("output")

        # 输入源:--db 与 --transcript 互斥;--session 仅搭 --db;都不给 → exit 2。
        if db is not None and transcript:
            console.print("[red]Error:[/red] --db 与 --transcript 互斥,二选一")
            raise typer.Exit(code=2)
        if session is not None and db is None:
            console.print("[red]Error:[/red] --session 仅与 --db 搭配使用")
            raise typer.Exit(code=2)
        if db is None and not transcript:
            console.print("[red]Error:[/red] 需要 --transcript 或 --db 之一")
            raise typer.Exit(code=2)

        transcript = transcript or []
        if db is not None:
            # --db 解析:list/选 session → export → 临时 json;多 session 在此 exit 2。
            # 解析后 transcript = [临时 json],后续 --kind/引擎照常走(opencode .json 经
            # rebuild 的 is_opencode_session 分派 → build_digest_from_opencode)。
            export_tmp = _audit_from_db(db, session)
            transcript = [export_tmp]

        # 结构化 records 输入(--transcript 指向 {format:"skill-eval-records"} 的 JSON):
        # 展开成临时 cc-jsonl 替进去,后续 --kind 切分 / rebuild 全按 cc 路径走,零改动。
        # producer(insight 等)因此不必手拼 cc-jsonl(免 tool_use/tool_result 拆分、Skill 大小写)。
        if any(is_structured_records(tp) for tp in transcript):
            expanded: list[Path] = []
            for tp in transcript:
                if not is_structured_records(tp):
                    expanded.append(tp)
                    continue
                recs = load_structured_records(tp)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
                ) as f:
                    f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs))
                expanded_tmps.append(Path(f.name))
                expanded.append(Path(f.name))
            transcript = expanded

        # --kind:切本声明单元被调用/被 dispatch 期间的段,每段写一个子段文件
        # (.jsonl=cc / .json=opencode),再喂 TranscriptAuditor(已支持多 transcript 聚合)。
        # 分派:transcript **格式**选切片器——opencode(单锚点 info.agent,无 Skill tool_use)
        # → slice_opencode_by_agent(kind=skill/agent 按声明名切;kind=root 取 session.info.agent);
        #   cc/structured(三锚点)→ slice_transcript(kind 选 root/skill/agent 锚点,不并集)。
        # 文件名带 transcript 级序号 ti:多个 --transcript 可能同名 stem,仅靠内层 i 会碰撞 → 后者覆盖前者。
        if kind:
            from skill_eval.audit.slice import slice_transcript
            from skill_eval.evals.opencode import (
                is_opencode_session,
                load_opencode_session,
                slice_opencode_by_agent,
            )

            # 先切所有段(暂存内存):确认有段再建目录,never-invoked → exit 2 不留空 slices/。
            # seg = (tp, ti, i, payload, is_oc);payload:cc=record 列表,opencode=子 session dict。
            segs: list[tuple[Path, int, int, object, bool]] = []
            for ti, tp in enumerate(transcript, 1):
                if is_opencode_session(tp):
                    # root:target 取 session.info.agent(opencode 无父子作用域,root 即主 agent);
                    # skill/agent:照旧按声明名(name)切。
                    if kind == "root":
                        sess = load_opencode_session(tp)
                        target_agent = (sess.get("info") or {}).get("agent")
                        if not target_agent:
                            console.print(
                                "[red]Error:[/red] --kind root:opencode session 缺 info.agent,"
                                "无法确定主 agent"
                            )
                            raise typer.Exit(code=2)
                    else:
                        target_agent = name
                    for i, seg in enumerate(slice_opencode_by_agent(tp, target_agent), 1):
                        segs.append((tp, ti, i, seg, True))
                else:
                    for i, seg in enumerate(slice_transcript(tp, name, kind), 1):
                        segs.append((tp, ti, i, seg, False))
            if not segs:
                # root 时 name(declared skill 名)对 root 无意义(root 按 parent_tool_use_id /
                # session.info.agent 切,不按 name 契约)→ 不提 declared name。注:cc root 主作用域
                # 在非空 transcript 下恒非空、opencode 缺 info.agent 已走前面 exit 2,故 root 时
                # 此分支 latent in 非空 transcript(完全空 transcript 仍触发:无 record → 主作用域
                # 也为空);防御性文案,不引入行为变化。
                who = f"'{name}'" if kind != "root" else "主 agent 作用域"
                console.print(
                    f"[red]Error:[/red] {who} 未在此 transcript 中以 --kind {kind} 锚点出现"
                    f"(无可对账段)"
                )
                raise typer.Exit(code=2)
            slice_dir = out / "slices"
            slice_dir.mkdir(parents=True, exist_ok=True)
            sliced: list[Path] = []
            for tp, ti, i, seg, is_oc in segs:
                ext = ".json" if is_oc else ".jsonl"
                sp = slice_dir / f"{tp.stem}-{name}-{ti}-{i}{ext}"
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

        # on_progress:audit 每批 LLM 判定 40-257s,边跑边报进度(transcript 头 + 每批 [k/总] + 耗时),
        # 否则全程静默、用户不知是否在跑。console.print 即时 flush,长跑也实时可见。
        report = TranscriptAuditor().audit(
            name, body, transcript, model=model,
            refine_instructions=refine_instructions,
            locate_turns=locate_turns,
            on_progress=lambda msg: console.print(msg),
        )
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
    finally:
        # --db 导出的临时 json:引擎读完即清(用户要留存走手工 opencode export)
        if export_tmp is not None:
            export_tmp.unlink(missing_ok=True)
        for t in expanded_tmps:
            t.unlink(missing_ok=True)

    # transcript 名 → 来源(cc/opencode/...);摘要 + findings 表标注用。同序 zip,同名 last-wins。
    src_of = dict(zip(report.transcripts, report.transcript_sources))

    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "audit-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # HTML 报告(与 JSON 并列,always 写;对齐路径3 scoring report 视觉)
    from skill_eval.reporting.audit_viewer import AuditReportGenerator
    html_path = out / "audit-report.html"
    AuditReportGenerator().write_html(report, html_path)

    summary = report.summary
    total = summary.get("total", 0)
    n_pass = summary.get("pass", 0)
    n_fail = summary.get("fail", 0)
    n_na = summary.get("na", 0)

    if format == "json":
        console.print(report.model_dump_json(indent=2))
        console.print(f"Written to {report_path}")
        return

    src_kinds = sorted(set(report.transcript_sources))
    src_tail = f"  [dim]sources: {' · '.join(src_kinds)}[/dim]" if src_kinds else ""
    console.print(
        f"Audited {len(report.transcripts)} transcript(s) for [bold]{name}[/bold]: "
        f"total={total} "
        f"pass=[green]{n_pass}[/green] "
        f"fail=[red]{n_fail}[/red] "
        f"na=[yellow]{n_na}[/yellow]{src_tail}"
    )

    tu = report.token_usage
    if tu.get("calls"):
        cache_read = tu.get("cache_read_input_tokens", 0)
        console.print(
            f"[dim]LLM tokens:[/dim] input={tu.get('input_tokens', 0)} "
            f"output={tu.get('output_tokens', 0)}"
            + (f" cache_read={cache_read}" if cache_read else "")
            + f" [dim]· cost=${tu.get('total_cost_usd', 0):.4f} · {tu['calls']} call(s)[/dim]"
        )
    if report.duration_seconds:
        console.print(f"[dim]耗时:[/dim] {report.duration_seconds:.0f}s")

    if report.warnings:
        console.print("[yellow]Warnings:[/yellow]")
        for w in report.warnings:
            console.print(f"  [yellow]- {w}[/yellow]")

    # 多 transcript:展示指令级聚合。fail 与 N/A(场景不适用)语义不同 → 分两张表:
    #   违规表看 fail%/fail/total(系统性违规,如"NEVER fabricate 在 75% 会话被违反");
    #   场景不适用表看 na/total + 哪几段不适用。
    # 不再混在一张用 fail 列 —— 那会让 N/A 行显示 0% fail / fail/total 0/2,看着像有
    # fail、误导(N/A 不是 fail)。一条指令既有 fail 又有 na(某段违反、某段不适用)→ 两表都出现,
    # 各反映一面。全 PASS 仍隐藏(默认正常态,太多)。单 transcript 不显示(聚合无额外洞察)。
    if len(report.transcripts) > 1 and report.by_instruction:
        n = len(report.transcripts)

        fail_rows = [r for r in report.by_instruction if r.fail_count > 0]
        if fail_rows:  # 已按 fail_rate 降序
            t = Table(title=f"By instruction — 违规 FAIL (across {n} transcripts)")
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
                    f"[{color}]{row.fail_rate * 100:.0f}%[/{color}]",
                    _truncate(row.instruction_text),
                    row.category.value,
                    f"{row.fail_count}/{row.total}",
                    ", ".join(row.fail_in_transcripts) if row.fail_in_transcripts else "—",
                )
            console.print(t)

        na_rows = [r for r in report.by_instruction if r.na_count > 0]
        if na_rows:
            na_rows.sort(key=lambda r: (-r.na_count, r.instruction_text))  # 不适用的段多的在前
            t = Table(title=f"By instruction — 场景不适用 N/A (across {n} transcripts)")
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

    # 只列非 PASS 明细(PASS 是默认正常态,太多;FAIL/UNRESOLVED/INDETERMINATE 才是关注点,N/A 不适用也列)。
    non_pass = [f for f in report.findings if f.verdict != AuditVerdict.PASS]
    if non_pass:
        table = Table(title="Findings (FAIL / UNRESOLVED / INDETERMINATE / N/A)")
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
            table.add_row(
                f"[{color}]{f.verdict.value}[/{color}]",
                f.category.value,
                _truncate(f.instruction_text),
                tcol,
                _truncate(f.evidence),
            )
        console.print(table)

    console.print(f"Written to {report_path}")
    console.print(f"Report written to {html_path}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{html_path.resolve()}")
