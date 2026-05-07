"""Batch scheduler — orchestrates multi-skill evaluation."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from skill_eval.batch.dag import SkillDAG, build_dag
from skill_eval.batch.cascade import CascadeAnalyzer
from skill_eval.models import (
    NodeStatus,
    RunConfig,
    ScoringConfig,
    SkillEntry,
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
        order = self.dag.topological_order()
        total_skills = len(order)

        for skill_idx, skill_path in enumerate(order, 1):
            skill_dir = self.base_dir / skill_path
            if not (skill_dir / "SKILL.md").exists():
                results[skill_path] = NodeStatus.SKIPPED
                continue

            try:
                from skill_eval.execution.runner import ABTestRunner
                from skill_eval.evals.loader import EvalLoader
                from skill_eval.execution.benchmark import BenchmarkAggregator

                evals_path = skill_dir / "evals" / "evals.json"
                if not evals_path.exists():
                    evals_path = skill_dir / "evals.json"

                if not evals_path.exists():
                    results[skill_path] = NodeStatus.SKIPPED
                    continue

                loader = EvalLoader()
                eval_set = loader.load(evals_path)

                skill_workspace = workspace / "unit" / skill_path.replace("/", "_")
                self.run_config.output_dir = skill_workspace

                def make_batch_progress(skill_name, idx, total):
                    def on_run_progress(eval_name, config_name, eval_idx, total_evals, completed, total_runs, grading, run_duration=0.0, run_output=None):
                        if self.on_progress:
                            self.on_progress(
                                skill_name, idx, total,
                                eval_name, config_name, eval_idx, total_evals,
                                completed, total_runs, grading, run_duration,
                            )
                    return on_run_progress

                ab = ABTestRunner(self.run_config, self.scoring_config, on_progress=make_batch_progress(skill_path, skill_idx, total_skills))
                iteration_dir = ab.run_ab_test(skill_dir, eval_set)

                agg = BenchmarkAggregator()
                benchmark = agg.aggregate(iteration_dir, skill_dir)
                (iteration_dir / "benchmark.json").write_text(
                    json.dumps(benchmark.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                delta = benchmark.run_summary.get("delta", {})
                pr = delta.get("pass_rate", "+0.00")
                results[skill_path] = NodeStatus.PASSED if not pr.startswith("-") else NodeStatus.FAILED

            except Exception:
                results[skill_path] = NodeStatus.FAILED

            if fail_fast and results[skill_path] in (NodeStatus.FAILED,):
                for remaining in order[order.index(skill_path) + 1:]:
                    results[remaining] = NodeStatus.SKIPPED
                break

        return results

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
    ) -> dict:
        from skill_eval.integration.runner import run_integration_batch

        scenarios = []
        skill_keywords: dict[str, list[str]] = {}
        if config is not None:
            scenarios = getattr(config, "integration_scenarios", []) or []
            skill_keywords = getattr(config, "skill_keywords", {}) or {}
        integration_evals = self.manifest.integration_evals if hasattr(self.manifest, "integration_evals") else []
        if integration_evals:
            for ie in integration_evals:
                scenarios.append({
                    "name": getattr(ie, "id", "ie"),
                    "type": "routing",
                    "test_prompts": [{"prompt": ie.prompt, "should_trigger": ie.expected_skills_triggered}],
                })

        if not scenarios:
            return {"total": 0, "passed": 0, "failed": 0, "scenarios": []}

        return run_integration_batch(
            scenarios=scenarios,
            skill_keywords=skill_keywords,
            scenario_type=scenario_type,
            workspace_dir=workspace / "integration",
        )
