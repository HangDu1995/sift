"""Tests for L2 integration tests — chain, routing, contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.integration.runner import (
    run_chain_test,
    run_routing_test,
    run_contract_test,
    run_integration_batch,
)


class TestChainTest:
    def test_single_step_chain_pass(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        step_dir = workspace / "step-0-skill-a"
        step_dir.mkdir()
        (step_dir / "output.txt").write_text("result from A")
        result = run_chain_test(
            chain=["skill-a"],
            input_data={"prompt": "test"},
            workspace_dir=workspace,
            step_results={"skill-a": (True, step_dir)},
        )
        assert result["passed"] is True

    def test_chain_with_failure_propagates(self, tmp_path: Path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = run_chain_test(
            chain=["skill-a", "skill-b"],
            input_data={"prompt": "test"},
            workspace_dir=workspace,
            step_results={"skill-a": (False, None), "skill-b": None},
        )
        assert result["passed"] is False
        assert result["steps"][0]["status"] == "FAILED"
        assert result["steps"][1]["status"] == "SKIPPED"


class TestRoutingTest:
    def test_correct_routing_pass(self):
        result = run_routing_test(
            test_prompts=[
                {"prompt": "播放视频", "should_trigger": ["media-player"], "should_not_trigger": ["data-layer"]},
            ],
            skill_keywords={"media-player": ["视频", "播放", "media"], "data-layer": ["数据库", "CRUD"]},
        )
        assert result["passed"] is True
        assert result["results"][0]["triggered"] == ["media-player"]

    def test_wrong_routing_fail(self):
        result = run_routing_test(
            test_prompts=[
                {"prompt": "播放视频", "should_trigger": ["data-layer"], "should_not_trigger": ["media-player"]},
            ],
            skill_keywords={"media-player": ["视频", "播放"], "data-layer": ["数据库"]},
        )
        assert result["passed"] is False

    def test_should_not_trigger_violation(self):
        result = run_routing_test(
            test_prompts=[
                {"prompt": "数据库操作", "should_trigger": ["data-layer"], "should_not_trigger": ["media-player"]},
            ],
            skill_keywords={"media-player": ["操作"], "data-layer": ["数据库"]},
        )
        assert result["passed"] is False


class TestContractTest:
    def test_valid_artifact_pass(self, tmp_path: Path):
        out = tmp_path / "output"
        out.mkdir()
        (out / "manifest.json").write_text(json.dumps({"version": 1, "items": []}))
        result = run_contract_test(
            producer="skill-a",
            consumer="skill-b",
            artifact_path=str(out / "manifest.json"),
            checks=[
                {"type": "FILE_EXISTS"},
                {"type": "JSON_VALID"},
            ],
            output_dir=out,
        )
        assert result["passed"] is True

    def test_missing_artifact_fail(self, tmp_path: Path):
        out = tmp_path / "output"
        out.mkdir()
        result = run_contract_test(
            producer="skill-a",
            consumer="skill-b",
            artifact_path=str(out / "nonexistent.json"),
            checks=[{"type": "FILE_EXISTS"}],
            output_dir=out,
        )
        assert result["passed"] is False


class TestIntegrationBatch:
    def test_runs_scenarios_from_config(self):
        scenarios = [
            {"name": "basic-routing", "type": "routing",
             "test_prompts": [
                 {"prompt": "build UI", "should_trigger": ["ui-builder"], "should_not_trigger": []},
             ]},
        ]
        result = run_integration_batch(
            scenarios=scenarios,
            skill_keywords={"ui-builder": ["UI", "页面", "组件"]},
        )
        assert result["total"] == 1
        assert result["passed"] == 1

    def test_filter_by_type(self):
        scenarios = [
            {"name": "r1", "type": "routing", "test_prompts": []},
            {"name": "c1", "type": "contract", "producer": "a", "consumer": "b", "artifact": "x", "checks": []},
        ]
        result = run_integration_batch(
            scenarios=scenarios,
            skill_keywords={},
            scenario_type="routing",
        )
        assert result["total"] == 1
