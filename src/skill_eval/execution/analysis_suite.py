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

    first_comp = benchmark.comparisons[0]
    winner_label = first_comp.winner

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

    if winner_label == "A":
        winner_skill, loser_skill = skill_content, "(no skill)"
        winner_trans, loser_trans = ws_transcript, wos_transcript
    elif winner_label == "B":
        winner_skill, loser_skill = "(no skill)", skill_content
        winner_trans, loser_trans = wos_transcript, ws_transcript
    else:
        return None

    return analyzer.analyze_comparison(
        winner=winner_label,
        winner_skill=winner_skill,
        loser_skill=loser_skill,
        comparison_reasoning=first_comp.reasoning,
        winner_transcript=winner_trans,
        loser_transcript=loser_trans,
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
