"""Tests for batch modules — DAG, cascade, manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.batch.dag import SkillDAG, build_dag
from skill_eval.batch.cascade import CascadeAnalyzer
from skill_eval.batch.manifest import ManifestLoader
from skill_eval.errors import CyclicDependencyError, ManifestError
from skill_eval.models import NodeStatus, SkillEntry, FailureType


class TestDAG:
    def test_topological_order_no_deps(self):
        dag = SkillDAG()
        for s in ["a", "b", "c"]:
            dag.add_skill(SkillEntry(path=s, depends_on=[]))
        order = dag.topological_order()
        assert set(order) == {"a", "b", "c"}

    def test_topological_order_with_deps(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        dag.add_skill(SkillEntry(path="c", depends_on=["b"]))
        order = dag.topological_order()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_cyclic_dependency_raises(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=["b"]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        with pytest.raises(CyclicDependencyError):
            dag.topological_order()

    def test_get_dependencies(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        assert dag.get_dependencies("b") == ["a"]
        assert dag.get_dependencies("a") == []

    def test_get_dependents(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        assert dag.get_dependents("a") == ["b"]

    def test_build_dag_skips_disabled(self):
        skills = [
            SkillEntry(path="a", depends_on=[], disabled=True),
            SkillEntry(path="b", depends_on=[], disabled=False),
        ]
        dag = build_dag(skills)
        assert "a" not in dag.nodes
        assert "b" in dag.nodes


class TestCascadeAnalyzer:
    def test_root_cause(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        analyzer = CascadeAnalyzer()
        results = {"a": NodeStatus.FAILED}
        attr = analyzer.analyze(dag, results)
        assert attr["a"] == FailureType.ROOT_CAUSE

    def test_cascade(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        analyzer = CascadeAnalyzer()
        results = {"a": NodeStatus.FAILED, "b": NodeStatus.FAILED}
        attr = analyzer.analyze(dag, results)
        assert attr["a"] == FailureType.ROOT_CAUSE
        assert attr["b"] == FailureType.CASCADE

    def test_dependency_missing(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        dag.add_skill(SkillEntry(path="b", depends_on=["a"]))
        analyzer = CascadeAnalyzer()
        results = {"b": NodeStatus.SKIPPED}
        attr = analyzer.analyze(dag, results)
        assert attr["b"] == FailureType.DEPENDENCY_MISSING

    def test_all_pass(self):
        dag = SkillDAG()
        dag.add_skill(SkillEntry(path="a", depends_on=[]))
        analyzer = CascadeAnalyzer()
        results = {"a": NodeStatus.PASSED}
        attr = analyzer.analyze(dag, results)
        assert attr == {}


class TestManifestLoader:
    def test_load_valid(self, tmp_path: Path):
        manifest = {
            "name": "test",
            "skills": [
                {"path": "skill-a", "depends_on": [], "disabled": False},
                {"path": "skill-b", "depends_on": ["skill-a"], "disabled": False},
            ],
        }
        p = tmp_path / "manifest.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        loader = ManifestLoader()
        result = loader.load(p)
        assert result.name == "test"
        assert len(result.skills) == 2

    def test_load_missing(self, tmp_path: Path):
        loader = ManifestLoader()
        with pytest.raises(ManifestError):
            loader.load(tmp_path / "nope.json")

    def test_discover(self, tmp_path: Path):
        for name in ("skill-a", "skill-b", "other-dir"):
            d = tmp_path / name
            d.mkdir()
            if name.startswith("skill"):
                (d / "SKILL.md").write_text("---\nname: test\n---\nbody\n", encoding="utf-8")

        loader = ManifestLoader()
        manifest = loader.discover(tmp_path)
        assert len(manifest.skills) == 2
