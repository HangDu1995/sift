"""Batch scheduler — orchestrates multi-skill evaluation."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from skill_eval.batch.dag import build_dag
from skill_eval.models import (
    NodeStatus,
    RunConfig,
    ScoringConfig,
    SkillsManifest,
)


class BatchScheduler:
    def __init__(
        self,
        manifest: SkillsManifest,
        run_config: RunConfig,
        scoring_config: ScoringConfig,
        base_dir: Path,
        on_progress: Callable | None = None,
    ):
        self.manifest = manifest
        self.run_config = run_config
        self.scoring_config = scoring_config
        self.base_dir = base_dir
        self.dag = build_dag(manifest.skills)
        self.on_progress = on_progress

    def run_unit_tests(self, workspace: Path, fail_fast: bool = False) -> dict[str, NodeStatus]:
        results: dict[str, NodeStatus] = {}
        results_lock = threading.Lock()
        levels = self.dag.topological_levels()
        total_skills = sum(len(l) for l in levels)
        global_idx = [0]

        for level in levels:
            if len(level) == 1:
                skill_path = level[0]
                with results_lock:
                    global_idx[0] += 1
                status = self._run_single_skill(
                    workspace, skill_path, global_idx[0], total_skills, fail_fast
                )
                with results_lock:
                    results[skill_path] = status
            else:
                with ThreadPoolExecutor(max_workers=min(len(level), 4)) as pool:
                    futures: dict = {}
                    for skill_path in level:
                        with results_lock:
                            global_idx[0] += 1
                        idx = global_idx[0]
                        futures[pool.submit(
                            self._run_single_skill,
                            workspace, skill_path, idx, total_skills, fail_fast
                        )] = skill_path
                    for future in as_completed(futures):
                        skill_path = futures[future]
                        try:
                            status = future.result()
                        except Exception as exc:
                            import traceback
                            print(f"[skill-eval batch] ERROR in {skill_path}: {exc}", flush=True)
                            traceback.print_exc()
                            status = NodeStatus.FAILED
                        with results_lock:
                            results[skill_path] = status

        return results

    def _run_single_skill(
        self, workspace: Path, skill_path: str, idx: int, total: int, fail_fast: bool
    ) -> NodeStatus:
        sys.stderr.write(f"[{idx}/{total}] {skill_path}: A/B test...\n")
        sys.stderr.flush()

        skill_dir = self.base_dir / skill_path
        if not (skill_dir / "SKILL.md").exists():
            return NodeStatus.SKIPPED

        try:
            from skill_eval.execution.runner import ABTestRunner
            from skill_eval.evals.loader import EvalLoader
            from skill_eval.execution.benchmark import BenchmarkAggregator

            evals_path = skill_dir / "evals" / "evals.json"
            if not evals_path.exists():
                evals_path = skill_dir / "evals.json"
            if not evals_path.exists():
                return NodeStatus.SKIPPED

            loader = EvalLoader()
            eval_set = loader.load(evals_path)

            skill_workspace = workspace / "unit" / skill_path.replace("/", "_")
            run_config = self.run_config.model_copy(update={"output_dir": skill_workspace})

            def make_batch_progress(skill_name, skill_idx, skill_total):
                def on_run_progress(eval_name, config_name, eval_idx, total_evals, completed, total_runs, grading, run_duration=0.0, run_output=None, phase="", substep=""):
                    if self.on_progress:
                        self.on_progress(
                            skill_name, skill_idx, skill_total,
                            eval_name, config_name, eval_idx, total_evals,
                            completed, total_runs, grading, run_duration,
                        )
                return on_run_progress

            ab = ABTestRunner(run_config, self.scoring_config, on_progress=make_batch_progress(skill_path, idx, total))
            iteration_dir = ab.run_ab_test(skill_dir, eval_set)

            agg_start = time.time()
            sys.stderr.write(f"[{idx}/{total}] {skill_path}: aggregating...\n")
            sys.stderr.flush()
            agg = BenchmarkAggregator(model=run_config.model, timeout=self.scoring_config.llm_timeout, enable_llm=self.scoring_config.enable_llm)
            benchmark = agg.aggregate(iteration_dir, skill_dir)
            (iteration_dir / "benchmark.json").write_text(
                json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            delta = benchmark.run_summary.get("delta", {})
            pr = delta.get("pass_rate", "+0.00")
            is_fail = (isinstance(pr, str) and pr.startswith("-")) or (isinstance(pr, (int, float)) and pr < 0)
            status = NodeStatus.PASSED if not is_fail else NodeStatus.FAILED

            agg_elapsed = time.time() - agg_start
            sys.stderr.write(f"[{idx}/{total}] {skill_path}: done ({status.value}, delta {pr}, {agg_elapsed:.0f}s)\n")
            sys.stderr.flush()
            return status

        except Exception as exc:
            import traceback
            print(f"[skill-eval batch] ERROR in {skill_path}: {exc}", flush=True)
            traceback.print_exc()
            return NodeStatus.FAILED
