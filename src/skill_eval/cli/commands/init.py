from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.evals.inspect import same_prompt_groups
from skill_eval.parser import resolve_skill_dir


@app.command()
def init(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output directory for evals.json"),
    generate_prompts: bool = typer.Option(True, "--generate-prompts/--no-generate-prompts", help="Use LLM to generate L1 unit-level evals"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for LLM generation"),
    num_evals: Optional[int] = typer.Option(None, "--num-evals", help="Max number of eval cases to generate"),
    from_transcript: Optional[Path] = typer.Option(
        None, "--from-transcript",
        help="从真实 session transcript 挖 evals(加料 SKILL.md,非替代)",
    ),
    kind: Optional[str] = typer.Option(
        None, "--kind",
        help="声明单元种类:只挖本 skill/agent 被调用/被 dispatch 期间段(skill=按 Skill 调用切;"
             "agent=按 agent 归属切,同 audit --kind)",
    ),
    timeout: int = typer.Option(
        600, "--timeout",
        help="LLM 调用超时秒数(mining 是单次大 prompt,默认 600;撞超时 → degraded fallback)",
    ),
    append: bool = typer.Option(
        False, "--append",
        help="追加到已有 evals.json(不覆盖):新 eval 的 id 接续已有 max+1。常配 --focus 定向补薄弱面。",
    ),
    focus: Optional[str] = typer.Option(
        None, "--focus",
        help="定向生成某一类 eval 补盲区:预设 negative-cases(反面场景)/ edge-cases(边界/错误),"
             "或任意自由文本(skill 特定方向)。常配 --append 增量补到已有 evals.json。",
    ),
    trigger_probes: bool = typer.Option(
        False, "--trigger-probes",
        help="生成 trigger-accuracy 探题(should/should-not 各 N 道)追加到 evals.json 的 trigger_probes 段。",
    ),
):
    """Initialize evals.json for a Skill.
    
    Generates L1 unit test cases from SKILL.md content.
    """
    skill_path = resolve_skill_dir(skill_path)
    from skill_eval.evals.generator import EvalInitGenerator

    try:
        gen = EvalInitGenerator(model=model, timeout=timeout, focus=focus)

        # 提前算 output 路径:append 模式要在生成前读已有 evals.json(喂定向补题 + 去重)
        if output is None:
            evals_dir = skill_path / "evals"
            evals_dir.mkdir(exist_ok=True)
            output = evals_dir / "evals.json"
        elif output.is_dir():
            output = output / "evals.json"

        # append 模式:提前读现有 evals。生成阶段喂 _existing_evals(定向补题 + 语义去重,
        # 治半聚焦/不去重);合并阶段复用 _existing_raw 保原题字节。读失败 → 降级覆盖写。
        _existing_raw = None
        _existing_evals = None
        if append and output.exists():
            from skill_eval.models import EvalSet
            try:
                _existing_raw = json.loads(output.read_text(encoding="utf-8"))
                _existing_evals = EvalSet.model_validate(_existing_raw).evals
            except Exception:
                console.print(f"[yellow]⚠️ 读已有 {output} 失败,改为覆盖写入。[/yellow]")

        if from_transcript:
            if kind is not None and kind not in ("skill", "agent"):
                console.print(f"[red]Error:[/red] --kind 只接受 skill / agent,收到 {kind!r}")
                raise typer.Exit(code=2)
            if not kind:
                from skill_eval.audit.slice import invoked_skills
                skills = invoked_skills(from_transcript)
                if len(skills) > 1:
                    console.print(
                        f"[yellow]⚠️  transcript 涉及 {len(skills)} 个 skill"
                        f"({', '.join(skills)}),会把全部当本 skill 挖、可能串味;"
                        f"加 --kind skill 只挖本 skill 被调用期间段。[/yellow]"
                    )
            console.print(f"Mining evals from transcript [bold]{from_transcript}[/bold]...")
            eval_set = gen.generate_from_transcript(
                skill_path, from_transcript,
                on_heartbeat=lambda m: console.print(m), kind=kind,
            )
        elif generate_prompts:
            with console.status("正在用 LLM 生成 L1 unit-level evals…", spinner="dots"):
                eval_set = gen.generate_with_llm(skill_path, existing_evals=_existing_evals)
        else:
            console.print("Generating template evals...")
            eval_set = gen.generate_template(skill_path)

        if num_evals and num_evals < len(eval_set.evals):
            console.print(f"  Trimming to {num_evals} evals (from {len(eval_set.evals)})")
            eval_set.evals = eval_set.evals[:num_evals]

        appended = 0
        new_evals_display: list = []   # append 模式:终端只列这些新增(B);非 append 为空 → 列全量
        _write_raw = None              # append 模式:保留原有字节的 raw dict(A);非 append 为 None → model_dump 写
        if append and _existing_raw is not None:
            from skill_eval.models import EvalSet
            existing = EvalSet.model_validate(_existing_raw)
            next_id = max((e.id for e in existing.evals), default=0) + 1
            new_evals = list(eval_set.evals)
            for e in new_evals:
                e.id = next_id
                next_id += 1
            appended = len(new_evals)
            # A:原有 raw evals 字节保留,只追加新题 model_dump(不重序列化原有 →
            # diff 干净、不丢 EvalCase 不认的未知字段)。
            _existing_raw.setdefault("evals", []).extend(
                e.model_dump(mode="json") for e in new_evals
            )
            eval_set = existing
            eval_set.evals.extend(new_evals)
            _write_raw = _existing_raw
            new_evals_display = new_evals

        if trigger_probes:
            # --trigger-probes 单用(无 --append)时默认覆盖会丢已有 scoring evals,
            # 显式载入已有 evals.json 保留原题(不重生成 scoring);--append 已合并则跳过。
            if output.exists() and not append:
                from skill_eval.models import EvalSet
                try:
                    eval_set = EvalSet.model_validate(json.loads(output.read_text(encoding="utf-8")))
                except Exception:
                    console.print(f"[yellow]⚠️ 读已有 {output} 失败,trigger 探题将附加到新生成的 evals。[/yellow]")
            with console.status("正在生成 trigger 探题…", spinner="dots"):
                new_probes = gen.generate_trigger_probes(skill_path, num_each=(num_evals or 5))
            if num_evals:  # 每方向上限 trim(不补下限)
                per = max(1, num_evals)
                should = [p for p in new_probes if p.expect_trigger][:per]
                should_not = [p for p in new_probes if not p.expect_trigger][:per]
                new_probes = should + should_not
            # id 接续已有 trigger_probes 的 max id + 1
            existing_probes = list(eval_set.trigger_probes)
            next_id = max((p.id for p in existing_probes), default=0) + 1
            for p in new_probes:
                p.id = next_id; next_id += 1
            eval_set.trigger_probes = existing_probes + new_probes
            if _write_raw is not None:  # append 模式:新 probe 也追加到 raw(保留原有 probe 字节)
                _write_raw.setdefault("trigger_probes", []).extend(
                    p.model_dump(mode="json") for p in new_probes
                )
            if new_probes:
                console.print(f"[green]Generated[/green] {len(new_probes)} trigger probes")
                # anti-leakage:should-trigger 探题若照抄 description 措辞,cc 可能靠字面匹配
                # 触发而非语义理解 → recall 虚高。只反馈(词面命中探题仍有诊断价值),不 retry。
                from skill_eval.parser import parse_skill_md
                from skill_eval.execution.trigger_runner import classify_phrase, PHRASE_LEAK_MIN_CHARS
                _desc = parse_skill_md(skill_path)[1]
                if _desc:
                    leaks = [p for p in new_probes
                             if p.expect_trigger and classify_phrase(p.prompt, _desc) == "literal"]
                    if leaks:
                        console.print(
                            f"[yellow]⚠ {len(leaks)}/{len(new_probes)} 道探题为词面命中"
                            f"(照抄 description ≥{PHRASE_LEAK_MIN_CHARS} 字符),"
                            f"建议人工复核或收紧 description。[/yellow]"
                        )
            else:
                console.print("[yellow]⚠️  未生成 trigger 探题(LLM 失败或解析空)。[/yellow]")

        # append 模式写 _write_raw(原有字节保留 + 新追加);否则 model_dump 全量。
        data = _write_raw if _write_raw is not None else eval_set.model_dump(mode="json")
        output.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        # B:append 模式终端只列新增(degraded / per-eval);非 append 列全量。
        display_evals = new_evals_display if new_evals_display else eval_set.evals

        if appended:
            # append 模式:显示「本次新增 N · 合并后 M」,不把合并总数当生成数(原 "Generated 12" 误导)
            console.print(f"本次新增 [green]{appended}[/green] eval · 合并后共 [bold]{len(eval_set.evals)}[/bold] 道 for [bold]{eval_set.skill_name}[/bold]")
        else:
            console.print(f"Generated [green]{len(eval_set.evals)}[/green] eval cases for [bold]{eval_set.skill_name}[/bold]")
        tok = gen.llm_usage.get("input_tokens", 0) + gen.llm_usage.get("output_tokens", 0)
        if gen.llm_duration > 0 or tok:
            console.print(f"耗时 [bold]{gen.llm_duration:.1f}s[/bold] · {tok} tok")
        if appended:
            console.print(f"[green]Appended[/green] {appended} new evals to existing {output}(id 接续、原题保留)")
        degraded = [(e, f) for e in display_evals for f in (e.flags or []) if f.startswith("degraded:")]
        if degraded:
            console.print(f"[yellow]⚠️  {len(degraded)} 个 eval 为 degraded fallback(LLM 失败,未挖到结构化信号,assertions 可能为空):[/yellow]")
            for e, f in degraded:
                console.print(f"[yellow]   - {e.name}: {f}[/yellow]")
            console.print("[yellow]建议重跑,或检查 claude CLI 可用性。[/yellow]")
        unknown_warns = gen.init_warnings
        if unknown_warns:
            console.print(f"[yellow]⚠️  {len(unknown_warns)} 条 eval 含未知字段"
                          f"(LLM 写了 EvalCase 不认的字段,已忽略;可能拼错、考卷可能缺字段):[/yellow]")
            for w in unknown_warns:
                console.print(f"[yellow]   - {w}[/yellow]")
        for e in display_evals:
            level = getattr(e, 'level', 'L1')
            line = (f"  [{e.id}] [{level}] {e.name}: {len(e.assertions)} assertions, "
                    f"{len(e.expectations)} expectations"
                    + (f", {len(e.fixtures)} fixtures" if e.fixtures else ""))
            if e.confidence is not None:
                line += f" · conf {e.confidence:.2f}"
            console.print(line)
            rat = (e.rationale or "").strip()
            if rat:
                console.print(f"      ↳ {rat[:140]}")
        for g in same_prompt_groups(eval_set):
            prompt_short = g["prompt"][:60] + ("…" if len(g["prompt"]) > 60 else "")
            flag_note = ",带 split flag" if g["split_flag"] else ""
            console.print(
                f"[cyan]ℹ[/cyan] 含同 prompt 题组:「{prompt_short}」× {g['count']} 道"
                f"(不同 scenario{flag_note},非重复)"
            )
        console.print(f"Written to {output}")
    except SkillEvalError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code)
