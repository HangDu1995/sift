"""Tests for Pydantic models — validation and serialization."""

from __future__ import annotations

import json

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
    InstructionCheck,
    InstructionSource,
    InstructionPriority,
    CheckMethod,
    CalibrationReport,
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

    def test_grading_summary_dual_rate_from_expectations(self):
        # 双率:按 ExpectationResult.source 拆(与 compliance 双率 P0-1e 同构)。
        # programmatic(客观 Tier1/2)稳、可信;llm(主观 Tier3)漂,只看方向。
        exps = [
            ExpectationResult(text="客观1", passed=True, evidence="", source="programmatic"),
            ExpectationResult(text="客观2", passed=False, evidence="", source="programmatic"),
            ExpectationResult(text="主观1", passed=True, evidence="", source="llm"),
            ExpectationResult(text="错误", passed=False, evidence="", source="error"),
        ]
        s = GradingSummary.from_expectations(exps)
        # 总率(含 error,兼容历史)
        assert s.passed == 2 and s.total == 4 and abs(s.pass_rate - 0.5) < 1e-9
        # programmatic 双率(只数 source=programmatic)
        assert s.passed_programmatic == 1 and s.total_programmatic == 2
        assert abs(s.pass_rate_programmatic - 0.5) < 1e-9
        # llm 双率(只数 source=llm)
        assert s.passed_llm == 1 and s.total_llm == 1
        assert abs(s.pass_rate_llm - 1.0) < 1e-9

    def test_grading_summary_dual_rate_null_when_no_class(self):
        # 无该类 expectation → None(不报假 0),与 compliance 双率一致
        exps = [ExpectationResult(text="客观", passed=True, evidence="", source="programmatic")]
        s = GradingSummary.from_expectations(exps)
        assert s.pass_rate_programmatic == 1.0
        assert s.pass_rate_llm is None and s.total_llm is None


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

    def test_run_result_metrics_carries_dual_rate(self):
        """RunResultMetrics 带 pass_rate_programmatic/llm:per-(eval,run) 双率要透到 viewer
        (检查点总览 3-dim + 逐项表 3-dim 列),不再只埋在 Δ 卡聚合层。
        缺省 None = 该 run 无该类 expectation(诚实缺值,不伪造 0)。"""
        m = RunResultMetrics(pass_rate=0.8, passed=4, failed=1, total=5,
                             pass_rate_programmatic=1.0, pass_rate_llm=0.5)
        assert m.pass_rate_programmatic == 1.0
        assert m.pass_rate_llm == 0.5
        m2 = RunResultMetrics(pass_rate=0.8, passed=4, failed=1, total=5)
        assert m2.pass_rate_programmatic is None
        assert m2.pass_rate_llm is None


class TestCalibrationModels:
    def test_calibration_report_new_schema(self):
        from skill_eval.models import (
            CalibrationVerdict, HardViolation, ProcessStatus,
        )
        cr = CalibrationReport(
            verdict=CalibrationVerdict.HIGH_SCORE_SUSPECT,
            result_pass=True,
            process_status=ProcessStatus.VIOLATED,
            hard_violations=[HardViolation(
                category="prohibition", instruction_text="禁止编造错误码", evidence="E-FAKE-999",
            )],
            soft_notes={"step": 2},
            scoring_rate=0.85,
            scoring_rate_basis="programmatic",
            scoring_rate_programmatic=0.85,
            scoring_rate_llm=0.9,
            compliance_rate=0.5,
        )
        assert cr.verdict == CalibrationVerdict.HIGH_SCORE_SUSPECT
        assert cr.hard_violations[0].category == "prohibition"
        assert cr.soft_notes == {"step": 2}
        assert cr.scoring_rate_basis == "programmatic"
        assert cr.scoring_rate_programmatic == 0.85


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
            max_score=1,
        )
        data = ar.model_dump(mode="json")
        assert data["category"] == "CAPABILITY_UPLIFT"
