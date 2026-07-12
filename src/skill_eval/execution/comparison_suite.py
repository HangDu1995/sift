from __future__ import annotations
import json
from pathlib import Path
from skill_eval.models import ComparisonResult
from skill_eval.scoring.comparator import BlindComparator


def _load_grading(run_dir: Path) -> dict | None:
    grading_path = run_dir / "grading.json"
    if grading_path.exists():
        try:
            return json.loads(grading_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _collect_run_output(run_dir: Path) -> str:
    outputs_dir = run_dir / "outputs"
    if not outputs_dir.is_dir():
        grading = _load_grading(run_dir)
        if grading:
            return json.dumps(grading, indent=2, ensure_ascii=False)[:4000]
        return ""
    parts = []
    for f in sorted(outputs_dir.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                if len(content) > 2000:
                    content = content[:2000] + "\n... (truncated)"
                parts.append(f"=== {f.name} ===\n{content}")
            except Exception:
                parts.append(f"=== {f.name} === (binary)")
    return "\n\n".join(parts) if parts else ""


def _progress_bar(current, total, width) -> str:
    pct = min(current / total, 1.0) if total else 0
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    return f" {bar} {pct*100:.0f}%"


def _compare_with_retry(comparator, ws_output, wos_output, eval_prompt, expectations, max_retries=3):
    for attempt in range(max_retries):
        try:
            return comparator.compare(
                output_with=ws_output,
                output_without=wos_output,
                eval_prompt=eval_prompt,
                expectations=expectations if expectations else None,
            )
        except Exception:
            if attempt < max_retries - 1:
                import time
                time.sleep(2 ** attempt)
    return None


def run_comparisons(aggregator, iteration_dir: Path) -> list:
    import sys
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 盲比较只服务于主观题(LLM 打分);programmatic-only 无主观输出,跳过省 LLM 调用
    if not getattr(aggregator, "enable_llm", True):
        sys.stderr.write("comparison skipped: programmatic-only scoring (enable_llm=False)\n")
        sys.stderr.flush()
        return []

    with_skill_dir = iteration_dir / "with_skill"
    without_skill_dir = iteration_dir / "without_skill"
    if not with_skill_dir.is_dir() or not without_skill_dir.is_dir():
        return []

    comparator = BlindComparator(model=aggregator.model, timeout=aggregator.timeout)

    ws_runs = sorted(p for p in with_skill_dir.glob("run-*") if p.is_dir())
    ws_nested = sorted(p for p in with_skill_dir.glob("eval-*/run-*") if p.is_dir())
    if ws_nested:
        ws_runs = ws_nested
    wos_runs = sorted(p for p in without_skill_dir.glob("run-*") if p.is_dir())
    wos_nested = sorted(p for p in without_skill_dir.glob("eval-*/run-*") if p.is_dir())
    if wos_nested:
        wos_runs = wos_nested

    pairs = list(zip(ws_runs, wos_runs))
    total = len(pairs)
    if total == 0:
        return []

    completed_count = [0]
    failures = [0]
    lock = threading.Lock()
    start_time = time.time()

    def _progress_line():
        with lock:
            c = completed_count[0]
        pct = c / total * 100 if total else 0
        bar = _progress_bar(c, total, 20)
        elapsed = time.time() - start_time
        eta_str = ""
        if c > 0:
            eta_s = (elapsed / c) * (total - c)
            eta_str = f" ETA {eta_s/60:.0f}m{eta_s%60:02.0f}s"
        return f"\r[{c}/{total}] comparison {bar} {pct:.0f}%{eta_str}"

    def _do_comparison(idx, ws_run, wos_run):
        ws_output = _collect_run_output(ws_run)
        wos_output = _collect_run_output(wos_run)
        ws_grading = _load_grading(ws_run)

        merged_prompt_path = iteration_dir / "merged_prompt.txt"
        eval_prompt = merged_prompt_path.read_text(encoding="utf-8")[:2000] if merged_prompt_path.exists() else ""

        expectations = []
        if ws_grading:
            expectations = [e.get("text", "") for e in ws_grading.get("expectations", []) if e.get("text")]

        result = _compare_with_retry(comparator, ws_output, wos_output, eval_prompt, expectations)
        if result is None:
            with lock:
                failures[0] += 1
            result = ComparisonResult(winner="tie", reasoning="comparison failed after retries")

        comp_path = ws_run.parent.parent / f"comparison-run{ws_run.name.split('-')[1]}.json"
        comp_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        with lock:
            completed_count[0] += 1
        sys.stderr.write(_progress_line())
        sys.stderr.flush()
        return (idx, result)

    comparisons_dict: dict[int, ComparisonResult] = {}
    max_workers = min(total, 4)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        sys.stderr.write(_progress_line())
        sys.stderr.flush()
        futures = {pool.submit(_do_comparison, i, ws, wos): i for i, (ws, wos) in enumerate(pairs)}
        for future in as_completed(futures):
            idx, result = future.result()
            comparisons_dict[idx] = result

    comparisons = [comparisons_dict[i] for i in sorted(comparisons_dict)]
    status = f"\r[{total}/{total}] comparison done ({time.time()-start_time:.0f}s)"
    if failures[0]:
        status += f", {failures[0]} failed"
    sys.stderr.write(status + "\n")
    sys.stderr.flush()
    return comparisons
