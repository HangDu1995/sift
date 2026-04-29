"""Tests for Pydantic models — validation and serialization."""

from __future__ import annotations

import json

import pytest

from skill_eval.models import (
    Assertion,
    AssertionType,
    EvalCase,
    EvalSet,
    GradingResult,
    GradingSummary,
    ExpectationResult,
    BenchmarkData,
    BenchmarkMetadata,
    RunResultEntry,
    RunResultMetrics,
    ComplianceReport,
    ComplianceResultItem,
    InstructionCheck,
    InstructionSource,
    InstructionPriority,
    CheckMethod,
    CalibrationReport,
    ConfidenceGrade,
    SignalType,
    AnalysisReport,
    SkillCategory,
    CheckResult,
    StatsValue,
)


class TestEvalModels:
    def test_eval_case_default_name(self):
        ec = EvalCase(id=1, prompt="test")
        assert ec.get_name() == "eval-1"

    def test_eval_case_custom_name(self):
        ec = EvalCase(id=1, name="Custom", prompt="test")
        assert ec.get_name() == "Custom"

    def test_eval_set_validation(self):
        es = EvalSet(skill_name="test", evals=[
            EvalCase(id=1, prompt="p1"),
            EvalCase(id=2, prompt="p2"),
        ])
        assert len(es.evals) == 2

    def test_assertion_types(self):
        a = Assertion(name="test", type=AssertionType.FILE_EXISTS, params={"filename": "out.txt"})
        assert a.type == AssertionType.FILE_EXISTS


class TestGradingModels:
    def test_grading_result_roundtrip(self):
        gr = GradingResult(
            expectations=[
                ExpectationResult(text="test", passed=True, evidence="ev", source="llm"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        data = gr.model_dump(mode="json")
        assert data["expectations"][0]["text"] == "test"

        restored = GradingResult.model_validate(data)
        assert restored.expectations[0].passed is True

    def test_grading_json_field_names(self):
        gr = GradingResult(
            expectations=[
                ExpectationResult(text="t", passed=True, evidence="e"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        data = gr.model_dump(mode="json")
        assert "text" in data["expectations"][0]
        assert "passed" in data["expectations"][0]
        assert "evidence" in data["expectations"][0]
        assert "pass_rate" in data["summary"]


class TestBenchmarkModels:
    def test_benchmark_roundtrip(self):
        bd = BenchmarkData(
            metadata=BenchmarkMetadata(skill_name="test-skill"),
            runs=[
                RunResultEntry(
                    eval_id=1,
                    configuration="with_skill",
                    run_number=1,
                    result=RunResultMetrics(pass_rate=0.8, passed=4, failed=1, total=5),
                )
            ],
        )
        data = bd.model_dump(mode="json")
        text = json.dumps(data)
        restored = BenchmarkData.model_validate(json.loads(text))
        assert restored.metadata.skill_name == "test-skill"
        assert restored.runs[0].configuration == "with_skill"


class TestComplianceModels:
    def test_compliance_report(self):
        cr = ComplianceReport(
            skill_name="test",
            total_instructions=3,
            followed_count=2,
            compliance_rate=0.67,
            checks=[
                ComplianceResultItem(
                    instruction=InstructionCheck(
                        source=InstructionSource.HEADING_STEP,
                        instruction_text="Do X",
                        section="Workflow",
                        priority=InstructionPriority.REQUIRED,
                        check_method=CheckMethod.LLM_JUDGE,
                    ),
                    followed=True,
                    evidence="Step executed",
                    confidence=0.9,
                    source="llm",
                )
            ],
        )
        data = cr.model_dump(mode="json")
        assert data["compliance_rate"] == 0.67


class TestCalibrationModels:
    def test_calibration_report(self):
        cr = CalibrationReport(
            total_expectations=5,
            programmatic_only_count=1,
            llm_only_count=1,
            both_evaluated=3,
            agreement_count=3,
            agreement_rate=1.0,
            overall_confidence=0.8,
            grade=ConfidenceGrade.HIGH,
        )
        assert cr.grade == ConfidenceGrade.HIGH


class TestAnalysisModels:
    def test_analysis_report(self):
        ar = AnalysisReport(
            skill_name="test",
            skill_path="/tmp/test",
            category=SkillCategory.CAPABILITY_UPLIFT,
            checks=[
                CheckResult(name="description_length", passed=True, message="OK"),
            ],
            score=1,
            max_score=11,
        )
        data = ar.model_dump(mode="json")
        assert data["category"] == "CAPABILITY_UPLIFT"
