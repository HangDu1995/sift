"""Cascade failure analyzer — attributes root causes in batch failures."""

from __future__ import annotations

from skill_eval.models import FailureType, NodeStatus, SkillEntry
from skill_eval.batch.dag import SkillDAG


class CascadeAnalyzer:
    def analyze(self, dag: SkillDAG, results: dict[str, NodeStatus]) -> dict[str, FailureType]:
        attributions: dict[str, FailureType] = {}

        for skill_path, status in results.items():
            if status == NodeStatus.PASSED:
                continue

            if status == NodeStatus.FAILED:
                deps = dag.get_dependencies(skill_path)
                dep_failed = any(results.get(d) in (NodeStatus.FAILED, NodeStatus.CASCADE_FAILED) for d in deps)

                if not deps:
                    attributions[skill_path] = FailureType.ROOT_CAUSE
                elif dep_failed:
                    attributions[skill_path] = FailureType.CASCADE
                else:
                    attributions[skill_path] = FailureType.ROOT_CAUSE

            elif status == NodeStatus.CASCADE_FAILED:
                attributions[skill_path] = FailureType.CASCADE

            elif status == NodeStatus.SKIPPED:
                deps = dag.get_dependencies(skill_path)
                dep_missing = any(d not in results or results.get(d) == NodeStatus.SKIPPED for d in deps)
                if dep_missing:
                    attributions[skill_path] = FailureType.DEPENDENCY_MISSING
                else:
                    attributions[skill_path] = FailureType.INDEPENDENT

        return attributions
