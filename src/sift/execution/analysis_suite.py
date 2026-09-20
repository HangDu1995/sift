from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from sift.models import AnalysisResult, BenchmarkData
from sift.scoring.prompts import ComparisonAnalysisInput
from sift.text_output import write_text


class _ComparisonAnalysisTransient(Exception):
    """analyze_comparison 被调用(过了结构检查:清晰赢家 + 有 transcript)却没产出可解析
    结果——瞬时失败(http 抖动/空响应/偶发不产 tool_use)。交 run_analysis 的应用层重试
    循环兜底(call_llm_with_retry 只重 429/error、不重空响应)。区别于结构性 None
    (平局/无赢家/无比较,在 early-return 阶段、不抛此异常、不重试)。
    """


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
            logging.getLogger(__name__).debug("Optional operation failed; continuing with fallback", exc_info=True)
    return ""


def run_comparison_analysis(
    iteration_dir: Path,
    benchmark: BenchmarkData,
    analyzer,
    on_usage=None,
    on_duration=None,
) -> AnalysisResult | None:
    from sift.parser import parse_skill_md

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
    majority = _comparison_majority(benchmark)
    if majority is None:
        return None
    winner_label, valid_winners, with_cnt, without_cnt = majority

    ws_runs = _comparison_run_paths(iteration_dir / "with_skill")
    wos_runs = _comparison_run_paths(iteration_dir / "without_skill")
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
    else:  # _comparison_majority 只返回 with 或 without。
        winner_skill, loser_skill = "(no skill)", skill_content
        winner_trans, loser_trans = wos_transcript, ws_transcript

    # 代表判词取**整体胜者侧赢**的一个比较(和 winner 一致),不再用 comparisons[0].reasoning
    # (可能和整体 winner 反)。ComparisonResult 无 eval/run 回指,无法把代表 comparison 映射到
    # 对应 transcript——transcript 仍取 config 第一个 run(上方 ws_runs[0]/wos_runs[0]),这是
    # 诚实局限;整体上下文靠下面的摘要 + 代表判词喂给 LLM。
    comparison_reasoning = _comparison_reasoning(benchmark, winner_label, valid_winners, with_cnt, without_cnt)

    _t0 = time.perf_counter()
    result = analyzer.analyze_comparison(
        on_usage=on_usage,
        comparison=ComparisonAnalysisInput(
            winner=winner_label,
            winner_skill=winner_skill,
            loser_skill=loser_skill,
            comparison_reasoning=comparison_reasoning,
            winner_transcript=winner_trans,
            loser_transcript=loser_trans,
            focus_skill=skill_content,
        ),
    )
    if on_duration:
        on_duration(time.perf_counter() - _t0)
    if result is None:
        # 走到这里 = 已过全部结构检查(清晰赢家 + 有 transcript),analyze_comparison 却
        # 返 None = LLM 调用瞬时失败(http 抖动/空响应/偶发不产 tool_use)。抛出交 run_analysis
        # 的应用层重试循环兜底(call_llm_with_retry 只重 429/error,不重空响应)。
        raise _ComparisonAnalysisTransient("analyze_comparison 无可解析结果(瞬时,可重试)")
    return result


def run_analysis(
    aggregator,
    iteration_dir: Path,
    benchmark: BenchmarkData,
    on_usage=None,
    on_duration=None,
) -> tuple:
    import sys

    from sift.scoring.analyzer import PosthocAnalyzer

    if not benchmark.runs:
        return [], None

    write_text("analysis: evaluating benchmark...\n", stream=sys.stderr)
    sys.stderr.flush()

    analyzer = PosthocAnalyzer(model=aggregator.model, timeout=aggregator.timeout)
    benchmark_json = json.dumps(benchmark.model_dump(mode="json"), ensure_ascii=False)[:12000]

    notes = None
    for attempt in range(3):
        try:
            _t0 = time.perf_counter()
            notes = analyzer.analyze_benchmark(benchmark_json, benchmark.metadata.skill_name, on_usage=on_usage)
            if on_duration:
                on_duration(time.perf_counter() - _t0)
            break
        except Exception:
            if attempt < 2:
                time.sleep(2**attempt)
    if notes:
        notes_path = iteration_dir / "analysis-notes.json"
        notes_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")

    comp_analysis = _analyze_comparisons(benchmark, iteration_dir, analyzer, on_usage, on_duration)

    write_text("analysis: done\n", stream=sys.stderr)
    sys.stderr.flush()

    return notes, comp_analysis


def _analyze_comparisons(benchmark, iteration_dir, analyzer, on_usage, on_duration):
    comp_analysis = None
    if not benchmark.comparisons:
        return None
    for attempt in range(3):
        try:
            comp_analysis = run_comparison_analysis(
                iteration_dir,
                benchmark,
                analyzer,
                on_usage=on_usage,
                on_duration=on_duration,
            )
            break
        except Exception:
            if attempt < 2:
                time.sleep(2**attempt)
    if comp_analysis:
        analysis_path = iteration_dir / "analysis-result.json"
        analysis_path.write_text(
            json.dumps(comp_analysis.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return comp_analysis


def _comparison_reasoning(benchmark, winner_label, valid_winners, with_cnt, without_cnt):
    """Explain the majority winner using a representative comparison from that side."""
    winner_keys = ("with", "A", "with_skill") if winner_label == "with" else ("without", "B", "without_skill")
    representative = next(
        (c for c in benchmark.comparisons if not c.failed and c.winner in winner_keys),
        None,
    )
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
    return comparison_reasoning


def _comparison_run_paths(config_dir: Path) -> list[Path]:
    """Prefer per-eval run paths when present, retaining the flat-layout fallback."""
    runs = sorted(config_dir.glob("run-*"))
    nested = sorted(config_dir.glob("eval-*/run-*"))
    return nested if nested else runs


def _comparison_majority(benchmark: BenchmarkData) -> tuple | None:
    """排除失败和无胜负结果；只有整体票数不相等时才有代表胜者。"""
    valid_winners = [c.winner for c in benchmark.comparisons if not c.failed]
    with_cnt = sum(1 for w in valid_winners if w in ("with", "A", "with_skill"))
    without_cnt = sum(1 for w in valid_winners if w in ("without", "B", "without_skill"))
    if with_cnt == 0 and without_cnt == 0:
        return None  # 无有效胜负(全 tie/failed)
    if with_cnt == without_cnt:
        return None  # 平局
    winner_label = "with" if with_cnt > without_cnt else "without"
    return winner_label, valid_winners, with_cnt, without_cnt
