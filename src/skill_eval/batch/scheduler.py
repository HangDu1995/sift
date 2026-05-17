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
from skill_eval.batch.cascade import CascadeAnalyzer
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

        for level_idx, level in enumerate(levels, 1):
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
                agg = BenchmarkAggregator(model=run_config.model, timeout=self.scoring_config.llm_timeout)
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

    def generate_cascade_report(self, results: dict[str, NodeStatus]) -> dict:
        analyzer = CascadeAnalyzer()
        attributions = analyzer.analyze(self.dag, results)

        report = {
            "total_skills": len(self.manifest.skills),
            "passed": sum(1 for s in results.values() if s == NodeStatus.PASSED),
            "failed": sum(1 for s in results.values() if s == NodeStatus.FAILED),
            "skipped": sum(1 for s in results.values() if s in (NodeStatus.SKIPPED, NodeStatus.CASCADE_FAILED)),
            "attributions": {k: v.value for k, v in attributions.items()},
        }
        return report

    def run_integration_tests(
        self,
        workspace: Path,
        config: object | None = None,
        scenario_type: str | None = None,
        pipeline_name: str | None = None,
        pipeline_steps: list[str] | None = None,
        project_overrides: list[str] | None = None,
    ) -> dict:
        from skill_eval.integration.runner import run_integration_batch

        scenarios = []
        skill_keywords: dict[str, list[str]] = {}
        pipelines: list[dict] = []
        if config is not None:
            scenarios = getattr(config, "integration_scenarios", []) or []
            skill_keywords = getattr(config, "skill_keywords", {}) or {}
            pipelines = getattr(config, "pipelines", []) or []
        integration_evals = self.manifest.integration_evals if hasattr(self.manifest, "integration_evals") else []
        if integration_evals:
            for ie in integration_evals:
                scenarios.append({
                    "name": getattr(ie, "id", "ie"),
                    "type": "routing",
                    "test_prompts": [{"prompt": ie.prompt, "should_trigger": ie.expected_skills_triggered}],
                })

        if not scenarios and not pipelines:
            return {"total": 0, "passed": 0, "failed": 0, "scenarios": []}

        skills_dir = self.base_dir
        skills_map: dict[str, Path] = {}
        for skill in self.manifest.skills:
            sp = self.base_dir / (skill.path if hasattr(skill, "path") else str(skill))
            if (sp / "SKILL.md").exists():
                name = getattr(skill, "name", sp.name)
                skills_map[name] = sp

        return run_integration_batch(
            scenarios=scenarios,
            skill_keywords=skill_keywords,
            scenario_type=scenario_type,
            workspace_dir=workspace / "integration",
            pipelines=pipelines if pipelines else None,
            pipeline_name=pipeline_name,
            pipeline_steps=pipeline_steps,
            project_overrides=project_overrides,
            skills_dir=skills_dir,
            skills_map=skills_map,
        )

    def run_regression_tests(self, workspace: Path, baseline: Path) -> dict:
        if not baseline.exists():
            return {"total": 0, "passed": 0, "failed": 0, "regressions": [], "error": "Baseline file not found"}

        try:
            baseline_data = json.loads(baseline.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"total": 0, "passed": 0, "failed": 0, "regressions": [], "error": str(exc)}

        baseline_runs = baseline_data.get("runs", [])
        baseline_by_skill: dict[str, float] = {}
        for run in baseline_runs:
            name = run.get("eval_name", "")
            config = run.get("configuration", "")
            if config == "with_skill":
                pr = run.get("result", {}).get("pass_rate", 0.0)
                baseline_by_skill[name] = pr

        current_benchmarks: dict[str, float] = {}
        unit_dir = workspace / "unit"
        if unit_dir.is_dir():
            for unit_sub in unit_dir.iterdir():
                for bench_file in unit_sub.rglob("benchmark.json"):
                    try:
                        bd = json.loads(bench_file.read_text(encoding="utf-8"))
                        for run in bd.get("runs", []):
                            name = run.get("eval_name", "")
                            config = run.get("configuration", "")
                            if config == "with_skill":
                                current_benchmarks[name] = run.get("result", {}).get("pass_rate", 0.0)
                    except Exception:
                        continue

        regressions: list[dict] = []
        passed = 0
        failed = 0
        all_skills = set(baseline_by_skill.keys()) | set(current_benchmarks.keys())

        for skill_name in sorted(all_skills):
            baseline_pr = baseline_by_skill.get(skill_name)
            current_pr = current_benchmarks.get(skill_name)
            entry: dict = {"skill": skill_name, "baseline_pass_rate": baseline_pr, "current_pass_rate": current_pr}

            if baseline_pr is None or current_pr is None:
                entry["status"] = "MISSING"
                entry["delta"] = None
                passed += 1
            else:
                delta = current_pr - baseline_pr
                entry["delta"] = round(delta, 4)
                if current_pr >= baseline_pr:
                    entry["status"] = "STABLE" if abs(delta) <= 0.05 else "IMPROVED"
                    passed += 1
                elif delta < -0.05:
                    entry["status"] = "REGRESSED"
                    failed += 1
                else:
                    entry["status"] = "STABLE"
                    passed += 1

                if baseline_pr > 0 and current_pr <= baseline_pr:
                    entry["redundant"] = True

            regressions.append(entry)

        return {
            "total": len(regressions),
            "passed": passed,
            "failed": failed,
            "regressions": regressions,
        }
