"""Tests for batch modules — DAG, manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.batch.dag import SkillDAG, build_dag
from skill_eval.batch.manifest import ManifestLoader
from skill_eval.errors import CyclicDependencyError, ManifestError
from skill_eval.models import NodeStatus, SkillEntry
from skill_eval.reporting.batch_viewer import _is_flat_delta, collect_batch_data


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


class TestBatchFlatDelta:
    """D6 趋零解读:客观 pass_rate_programmatic |Δ|≤0.05 → flat_programmatic 标记
    (batch DAG/表格 delta 旁显 ⚠)。客观为主(稳);缺值 None → False(诚实,不报假趋零)。
    与 viewer.html 的 isFlatDelta 同逻辑/同阈值(0.05,对齐 dClr 染色边界)。"""

    def test_is_flat_delta_threshold(self):
        assert _is_flat_delta("+0.02") is True
        assert _is_flat_delta("-0.05") is True     # 边界 ≤
        assert _is_flat_delta("+0.30") is False
        assert _is_flat_delta("+0.06") is False
        assert _is_flat_delta(None) is False        # 缺值诚实
        assert _is_flat_delta("abc") is False       # 非数

    def test_collect_batch_data_marks_flat_programmatic(self, tmp_path: Path):
        """端到端:workspace/unit/<skill>/benchmark.json 的客观 Δ 趋零 → flat_programmatic True。"""

        def _skill(name: str, prog_delta):
            d = tmp_path / "unit" / name
            d.mkdir(parents=True)
            (d / "benchmark.json").write_text(json.dumps({
                "metadata": {"skill_name": name},
                "runs": [],
                "run_summary": {
                    "with_skill": {"pass_rate": {"mean": 0.5}},
                    "without_skill": {"pass_rate": {"mean": 0.5}},
                    "delta": {"pass_rate": "+0.02", "pass_rate_programmatic": prog_delta},
                },
            }), encoding="utf-8")

        _skill("flat", "-0.02")    # 客观趋零
        _skill("gain", "+0.30")    # 客观显著
        _skill("none", None)       # 客观缺值

        data = collect_batch_data(tmp_path)
        by = {s["name"]: s for s in data["skills"]}
        assert by["flat"]["flat_programmatic"] is True
        assert by["gain"]["flat_programmatic"] is False
        assert by["none"]["flat_programmatic"] is False   # 缺值诚实,不报假趋零
