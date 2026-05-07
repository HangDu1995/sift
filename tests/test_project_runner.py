"""Tests for pipeline runner — project copy, dependency resolution, pipeline execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_eval.integration.project_runner import (
    copy_project,
    resolve_dependencies,
    resolve_project_path,
    run_pipeline,
    run_pipeline_step,
)


class TestResolveDependencies:
    def test_empty_requested_returns_all(self):
        steps = [
            {"name": "a", "skill": "s1"},
            {"name": "b", "skill": "s2"},
        ]
        result = resolve_dependencies([], steps)
        assert len(result) == 2

    def test_single_step_no_deps(self):
        steps = [
            {"name": "a", "skill": "s1"},
            {"name": "b", "skill": "s2"},
            {"name": "c", "skill": "s3"},
        ]
        result = resolve_dependencies(["b"], steps)
        assert len(result) == 1
        assert result[0]["name"] == "b"

    def test_transitive_deps_included(self):
        steps = [
            {"name": "a", "skill": "s1", "depends_on": []},
            {"name": "b", "skill": "s2", "depends_on": ["a"]},
            {"name": "c", "skill": "s3", "depends_on": ["b"]},
        ]
        result = resolve_dependencies(["c"], steps)
        names = [s["name"] for s in result]
        assert "a" in names
        assert "b" in names
        assert "c" in names
        assert len(result) == 3

    def test_diamond_dependency(self):
        steps = [
            {"name": "root", "skill": "s1"},
            {"name": "left", "skill": "s2", "depends_on": ["root"]},
            {"name": "right", "skill": "s3", "depends_on": ["root"]},
            {"name": "merge", "skill": "s4", "depends_on": ["left", "right"]},
        ]
        result = resolve_dependencies(["merge"], steps)
        names = [s["name"] for s in result]
        assert len(names) == 4
        assert set(names) == {"root", "left", "right", "merge"}

    def test_preserves_order(self):
        steps = [
            {"name": "step-1", "skill": "s1"},
            {"name": "step-2", "skill": "s2", "depends_on": ["step-1"]},
            {"name": "step-3", "skill": "s3", "depends_on": ["step-2"]},
        ]
        result = resolve_dependencies(["step-3"], steps)
        assert [s["name"] for s in result] == ["step-1", "step-2", "step-3"]

    def test_nonexistent_step_ignored(self):
        steps = [
            {"name": "a", "skill": "s1"},
        ]
        result = resolve_dependencies(["a", "ghost"], steps)
        assert len(result) == 1
        assert result[0]["name"] == "a"


class TestCopyProject:
    def test_copies_directory(self, tmp_path: Path):
        src = tmp_path / "src_proj"
        src.mkdir()
        (src / "build.gradle").write_text("plugins { }")
        (src / "settings.gradle").write_text("rootProject.name = 'test'")
        app = src / "app" / "src"
        app.mkdir(parents=True)
        (app / "Main.java").write_text("class Main {}")

        dest = tmp_path / "copy_proj"
        result = copy_project(src, dest)

        assert (result / "build.gradle").exists()
        assert (result / "app" / "src" / "Main.java").read_text() == "class Main {}"

    def test_overwrites_existing(self, tmp_path: Path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("new")

        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "old.txt").write_text("old")

        copy_project(src, dest)
        assert (dest / "file.txt").exists()
        assert not (dest / "old.txt").exists()


class TestResolveProjectPath:
    def test_absolute_path(self):
        p = Path("/tmp/some-project")
        result = resolve_project_path(str(p))
        assert result == p

    def test_relative_with_skills_dir(self, tmp_path: Path):
        proj = tmp_path / "fixtures" / "my-proj"
        proj.mkdir(parents=True)
        result = resolve_project_path("fixtures/my-proj", skills_dir=tmp_path)
        assert result == proj

    def test_bundled_fallback(self):
        result = resolve_project_path("fixtures/projects/android-minimal")
        assert result.name == "android-minimal"


class TestRunPipelineStep:
    def test_basic_step_pass(self):
        step = {"name": "build", "skill": "android-builder", "depends_on": []}
        result = run_pipeline_step(step, project_dir=Path("/tmp/proj"))
        assert result["passed"] is True
        assert result["name"] == "build"

    def test_failed_dependency_skips(self):
        step = {"name": "test", "skill": "tester", "depends_on": ["build"]}
        prev = {"build": {"passed": False, "name": "build"}}
        result = run_pipeline_step(step, project_dir=Path("/tmp/proj"), step_results=prev)
        assert result["passed"] is False
        assert result["status"] == "SKIPPED"


class TestRunPipeline:
    def test_full_pipeline(self, tmp_path: Path):
        src = tmp_path / "project"
        src.mkdir()
        (src / "build.gradle").write_text("// project")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {
            "name": "test-pipeline",
            "project": str(src),
            "steps": [
                {"name": "s1", "skill": "skill-a", "depends_on": []},
                {"name": "s2", "skill": "skill-b", "depends_on": ["s1"]},
            ],
        }
        result = run_pipeline(pipeline, workspace)
        assert result["name"] == "test-pipeline"
        assert result["passed"] == 1
        assert len(result["project_results"]) == 1

    def test_selective_steps_with_deps(self, tmp_path: Path):
        src = tmp_path / "project"
        src.mkdir()
        (src / "build.gradle").write_text("// project")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {
            "name": "dep-pipeline",
            "project": str(src),
            "steps": [
                {"name": "a", "skill": "sa", "depends_on": []},
                {"name": "b", "skill": "sb", "depends_on": ["a"]},
                {"name": "c", "skill": "sc", "depends_on": ["b"]},
            ],
        }
        result = run_pipeline(pipeline, workspace, steps_to_run=["c"])
        pr = result["project_results"][0]
        assert pr["total_steps"] == 3
        assert pr["passed"] is True

    def test_multi_project(self, tmp_path: Path):
        p1 = tmp_path / "proj1"
        p1.mkdir()
        (p1 / "file.txt").write_text("one")
        p2 = tmp_path / "proj2"
        p2.mkdir()
        (p2 / "file.txt").write_text("two")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {
            "name": "multi",
            "projects": [str(p1), str(p2)],
            "steps": [
                {"name": "step1", "skill": "s1", "depends_on": []},
            ],
        }
        result = run_pipeline(pipeline, workspace)
        assert result["total"] == 2

    def test_no_project_returns_empty(self, tmp_path: Path):
        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {"name": "empty", "steps": [{"name": "s1", "skill": "s1"}]}
        result = run_pipeline(pipeline, workspace)
        assert result["total"] == 0
        assert result["passed"] == 0

    def test_project_override(self, tmp_path: Path):
        p1 = tmp_path / "default_proj"
        p1.mkdir()
        (p1 / "f.txt").write_text("default")

        override = tmp_path / "override_proj"
        override.mkdir()
        (override / "f.txt").write_text("override")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {
            "name": "override-test",
            "project": str(p1),
            "steps": [{"name": "s1", "skill": "s1", "depends_on": []}],
        }
        result = run_pipeline(pipeline, workspace, projects=[str(override)])
        assert result["total"] == 1
        assert (workspace / "override_proj" / "f.txt").exists()


class TestIntegrationBatchWithPipeline:
    def test_pipeline_in_batch(self, tmp_path: Path):
        from skill_eval.integration.runner import run_integration_batch

        src = tmp_path / "proj"
        src.mkdir()
        (src / "build.gradle").write_text("// test")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipeline = {
            "name": "batch-pipeline",
            "project": str(src),
            "steps": [
                {"name": "step-a", "skill": "skill-a", "depends_on": []},
            ],
        }
        result = run_integration_batch(
            scenarios=[],
            skill_keywords={},
            workspace_dir=workspace,
            pipelines=[pipeline],
        )
        assert result["total"] == 1
        assert result["passed"] == 1

    def test_filter_pipeline_by_name(self, tmp_path: Path):
        from skill_eval.integration.runner import run_integration_batch

        src = tmp_path / "proj"
        src.mkdir()
        (src / "f.txt").write_text("x")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        pipelines = [
            {"name": "wanted", "project": str(src), "steps": [{"name": "s1", "skill": "s1"}]},
            {"name": "unwanted", "project": str(src), "steps": [{"name": "s1", "skill": "s1"}]},
        ]
        result = run_integration_batch(
            scenarios=[],
            skill_keywords={},
            workspace_dir=workspace,
            pipelines=pipelines,
            pipeline_name="wanted",
        )
        assert result["total"] == 1
        assert result["scenarios"][0]["name"] == "wanted"
