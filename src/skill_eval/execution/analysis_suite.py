from __future__ import annotations
import json
from pathlib import Path
from skill_eval.models import AnalysisResult, BenchmarkData


def _load_transcript(run_dir: Path) -> str:
    transcript_path = run_dir / "transcript.md"
    if transcript_path.exists():
        return transcript_path.read_text(encoding="utf-8", errors="replace")[:4000]
    debug_path = run_dir / "run_debug.json"
    if debug_path.exists():
        try:
            debug = json.loads(debug_path.read_text(encoding="utf-8"))
            return debug.get("transcript_preview", "")[:4000]
        except Exception:
            pass
    return ""


def run_comparison_analysis(aggregator, iteration_dir: Path, benchmark: BenchmarkData, analyzer) -> AnalysisResult | None:
    from skill_eval.parser import parse_skill_md

    skill_path = Path(benchmark.metadata.skill_path) if benchmark.metadata.skill_path else None
    if not skill_path or not (skill_path / "SKILL.md").exists():
        return None

    _, _, _, _, skill_body = parse_skill_md(skill_path)
    skill_content = skill_body[:4000]

    if not benchmark.comparisons:
        return None

    # winner 取**整体多数票**(排除 failed/tie),不再用 comparisons[0]——它只是 (eval_name,
    # run_n) 字典序第一对,不代表整体(实证:整体 with 67% 胜,comparisons[0] 却可能是 with 输)。
    # 平局 / 无有效胜负 → 没代表性强弱,不分析。
    valid_winners = [c.winner for c in benchmark.comparisons if not c.failed]
    with_cnt = sum(1 for w in valid_winners if w in ("with", "A", "with_skill"))
    without_cnt = sum(1 for w in valid_winners if w in ("without", "B", "without_skill"))
    if with_cnt == 0 and without_cnt == 0:
        return None  # 无有效胜负(全 tie/failed)
    if with_cnt == without_cnt:
        return None  # 平局
    winner_label = "with" if with_cnt > without_cnt else "without"

    with_skill_dir = iteration_dir / "with_skill"
    without_skill_dir = iteration_dir / "without_skill"
    ws_runs = sorted(with_skill_dir.glob("run-*"))
    ws_nested = sorted(with_skill_dir.glob("eval-*/run-*"))
    if ws_nested:
        ws_runs = ws_nested
    wos_runs = sorted(without_skill_dir.glob("run-*"))
    wos_nested = sorted(without_skill_dir.glob("eval-*/run-*"))
    if wos_nested:
        wos_runs = wos_nested
    if not ws_runs or not wos_runs:
        return None

    ws_transcript = _load_transcript(ws_runs[0])
    wos_transcript = _load_transcript(wos_runs[0])

    # winner 是语义侧:position 随机化 + _translate 后 comparator 存 "with"/"without"/"tie"
    # (老数据可能是匿名 A/B)。只对清晰赢家(with/without 侧)做分析;tie/空 → 没赢家,返 None。
    # 旧代码只判 ==A/==B,语义时代永不命中 → 恒 None → 深度分析从未显示(stale-code bug)。
    if winner_label in ("A", "with", "with_skill"):
        winner_skill, loser_skill = skill_content, "(no skill)"
        winner_trans, loser_trans = ws_transcript, wos_transcript
    elif winner_label in ("B", "without", "without_skill"):
        winner_skill, loser_skill = "(no skill)", skill_content
        winner_trans, loser_trans = wos_transcript, ws_transcript
    else:
        return None

    # 代表判词取**整体胜者侧赢**的一个比较(和 winner 一致),不再用 comparisons[0].reasoning
    # (可能和整体 winner 反)。ComparisonResult 无 eval/run 回指,无法把代表 comparison 映射到
    # 对应 transcript——transcript 仍取 config 第一个 run(上方 ws_runs[0]/wos_runs[0]),这是
    # 诚实局限;整体上下文靠下面的摘要 + 代表判词喂给 LLM。
    winner_keys = ("with", "A", "with_skill") if winner_label == "with" else ("without", "B", "without_skill")
    representative = next((c for c in benchmark.comparisons if not c.failed and c.winner in winner_keys), None)
    rep_reasoning = representative.reasoning if representative else ""
    loser_label = "without" if winner_label == "with" else "with"
    winner_cnt, loser_cnt = (with_cnt, without_cnt) if winner_label == "with" else (without_cnt, with_cnt)
    tie_cnt = len(valid_winners) - with_cnt - without_cnt
    comparison_reasoning = (
        f"[整体分布] 共 {len(valid_winners)} 次有效比较,"
        f"{winner_label} 侧胜 {winner_cnt} 次、{loser_label} 侧胜 {loser_cnt} 次"
        f"(另有 tie/failed {tie_cnt} 次)。"
        f"以下为 {winner_label} 侧赢的一个代表性比较判词:\n{rep_reasoning}"
    )

    return analyzer.analyze_comparison(
        winner=winner_label,
        winner_skill=winner_skill,
        loser_skill=loser_skill,
        comparison_reasoning=comparison_reasoning,
        winner_transcript=winner_trans,
        loser_transcript=loser_trans,
        focus_skill=skill_content,   # 改进对象:恒 = 被测/candidate skill(不随 winner 侧变)
    )


def run_analysis(aggregator, iteration_dir: Path, benchmark: BenchmarkData) -> tuple:
    import sys
    from skill_eval.scoring.analyzer import PosthocAnalyzer

    if not benchmark.runs:
        return [], None

    sys.stderr.write("analysis: evaluating benchmark...\n")
    sys.stderr.flush()

    analyzer = PosthocAnalyzer(model=aggregator.model, timeout=aggregator.timeout)
    benchmark_json = json.dumps(benchmark.model_dump(mode="json"), ensure_ascii=False)[:12000]

    notes = None
    for attempt in range(3):
        try:
            notes = analyzer.analyze_benchmark(benchmark_json, benchmark.metadata.skill_name)
            break
        except Exception:
            if attempt < 2:
                import time
                time.sleep(2 ** attempt)
    if notes:
        notes_path = iteration_dir / "analysis-notes.json"
        notes_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")

    comp_analysis = None
    if benchmark.comparisons:
        for attempt in range(3):
            try:
                comp_analysis = run_comparison_analysis(aggregator, iteration_dir, benchmark, analyzer)
                break
            except Exception:
                if attempt < 2:
                    import time
                    time.sleep(2 ** attempt)
        if comp_analysis:
            analysis_path = iteration_dir / "analysis-result.json"
            analysis_path.write_text(
                json.dumps(comp_analysis.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    sys.stderr.write("analysis: done\n")
    sys.stderr.flush()

    return notes, comp_analysis
