"""DAG builder — constructs execution order from skill dependencies."""

from __future__ import annotations

from skill_eval.errors import CyclicDependencyError
from skill_eval.models import SkillEntry


class SkillDAG:
    def __init__(self):
        self.nodes: dict[str, SkillEntry] = {}
        self.edges: list[tuple[str, str]] = []

    def add_skill(self, entry: SkillEntry) -> None:
        self.nodes[entry.path] = entry
        for dep in entry.depends_on:
            self.edges.append((dep, entry.path))

    def topological_order(self) -> list[str]:
        in_degree: dict[str, int] = {n: 0 for n in self.nodes}
        adj: dict[str, list[str]] = {n: [] for n in self.nodes}

        for src, dst in self.edges:
            if src in adj:
                adj[src].append(dst)
            if dst in in_degree:
                in_degree[dst] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        order: list[str] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(self.nodes):
            raise CyclicDependencyError("Cyclic dependency detected in skill manifest")

        return order

    def get_dependencies(self, skill_path: str) -> list[str]:
        return [src for src, dst in self.edges if dst == skill_path]

    def get_dependents(self, skill_path: str) -> list[str]:
        return [dst for src, dst in self.edges if src == skill_path]


def build_dag(skills: list[SkillEntry]) -> SkillDAG:
    dag = SkillDAG()
    for skill in skills:
        if not skill.disabled:
            dag.add_skill(skill)
    return dag
