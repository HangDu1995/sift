from __future__ import annotations
import json
import time
from pathlib import Path
from skill_eval.models import ComparisonResult
from skill_eval.scoring.comparator import BlindComparator
from skill_eval.execution.run_paths import index_runs, is_nested


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


def _compare_with_retry(comparator, ws_output, wos_output, eval_prompt, expectations, label_a="with", label_b="without", max_retries=3, on_usage=None, on_duration=None):
    """盲比较带重试:compare() 返回 failed=True(调用/解析失败)时重试,成功即返回。

    失败判定靠 ComparisonResult.failed 标志(_parse 在调用/解析失败时设 True)。
    旧实现 try/except 重试,但 compare() 把失败吞成静默 TIE、永不抛 → 重试循环是
    死代码(实测 30% 失败率里无一条触发过重试,reasoning 全是 _parse 的降级串)。
    耗尽 max_retries 返回 None,由调用方降级 tie。
    """
    for attempt in range(max_retries):
        _t0 = time.perf_counter()
        result = comparator.compare(
            output_a=ws_output,
            output_b=wos_output,
            eval_prompt=eval_prompt,
            expectations=expectations if expectations else None,
            label_a=label_a,
            label_b=label_b,
            on_usage=on_usage,
        )
        if on_duration:
            on_duration(time.perf_counter() - _t0)
        if not getattr(result, "failed", False):
            return result
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    return None


def run_comparisons(aggregator, iteration_dir: Path, sides: list | None = None, on_usage=None, on_duration=None) -> list:
    import sys
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 盲比较只服务于主观题(LLM 打分);programmatic-only 无主观输出,跳过省 LLM 调用
    if not getattr(aggregator, "enable_llm", True):
        sys.stderr.write("comparison skipped: programmatic-only scoring (enable_llm=False)\n")
        sys.stderr.flush()
        return []

    # sides = [(config_name, inject_skill, comp_label), ...];None → 默认 with/without(向后兼容)
    if sides is None:
        sides = [("with_skill", None, "with"), ("without_skill", None, "without")]
    side_a_dir = iteration_dir / sides[0][0]
    side_b_dir = iteration_dir / sides[1][0]
    if not side_a_dir.is_dir() or not side_b_dir.is_dir():
        return []

    label_a, label_b = sides[0][2], sides[1][2]
    comparator = BlindComparator(model=aggregator.model, timeout=aggregator.timeout)

    # 按 (eval_name, run_n) 显式配对,各自取最终 retry 目录。
    # 旧 glob("eval-*/run-*")+zip 会把 retry 目录混入(run-1、run-1-retry-1 都匹配),
    # with 侧多出的 retry 排序后错位配给 without 的下一个 run → 跨 eval 伪 tie。
    ws_index = index_runs(side_a_dir, is_nested(side_a_dir))
    wos_index = index_runs(side_b_dir, is_nested(side_b_dir))
    common = sorted(set(ws_index) & set(wos_index))
    pairs = [(ws_index[k], wos_index[k]) for k in common]
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

        result = _compare_with_retry(comparator, ws_output, wos_output, eval_prompt, expectations, label_a, label_b, on_usage=on_usage, on_duration=on_duration)
        if result is None:
            with lock:
                failures[0] += 1
            result = ComparisonResult(winner="tie", reasoning="comparison failed after retries", failed=True)

        eval_name = ws_run.parent.name
        suffix = f"{eval_name}-" if eval_name.startswith("eval-") else ""
        comp_path = ws_run.parent.parent / f"comparison-{suffix}run{ws_run.name.split('-')[1]}.json"
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
