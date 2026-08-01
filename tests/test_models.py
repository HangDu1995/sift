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


class TestCalibrationSummary:
    """PerSideCalibration / CalibrationSummary 聚合:per-side 收 HIGH_SCORE_SUSPECT
    (gate 盲区) 的 hard_violations 明细;None side 保留(无 compliance)。"""

    def _rep(self, verdict, **kw):
        from skill_eval.models.batch_models import CalibrationReport
        from skill_eval.models.enums import ProcessStatus
        return CalibrationReport(
            verdict=verdict, result_pass=kw.get("result_pass", True),
            process_status=kw.get("process_status", ProcessStatus.OK),
            hard_violations=kw.get("hard_violations", []),
            soft_notes=kw.get("soft_notes", {}),
            scoring_rate=kw.get("scoring_rate", 1.0), scoring_rate_basis="programmatic",
            scoring_rate_programmatic=kw.get("scoring_rate_programmatic", 1.0),
            scoring_rate_llm=kw.get("scoring_rate_llm", 1.0),
            compliance_rate=kw.get("compliance_rate", 1.0),
        )

    def _hv(self):
        from skill_eval.models.batch_models import HardViolation
        return HardViolation(category="prohibition", instruction_text="引用日志原文行", evidence="未引用")

    def test_calibration_summary_with_without_mode(self):
        from skill_eval.models.batch_models import CalibrationSummary
        from skill_eval.models.enums import CalibrationVerdict
        per_side = {
            "with_skill": [
                (1, 1, self._rep(CalibrationVerdict.TRUSTED)),
                (1, 2, self._rep(CalibrationVerdict.HIGH_SCORE_SUSPECT, hard_violations=[self._hv()])),
            ],
            "without_skill": None,
        }
        s = CalibrationSummary.from_per_side(per_side)
        assert s.sides["without_skill"] is None
        ws = s.sides["with_skill"]
        assert ws.total == 2
        assert ws.verdict_counts == {"TRUSTED": 1, "HIGH_SCORE_SUSPECT": 1}
        assert len(ws.high_score_suspects) == 1
        sus = ws.high_score_suspects[0]
        assert (sus["eval"], sus["run"]) == (1, 2)
        assert sus["category"] == "prohibition"

    def test_calibration_summary_v1v2_both_sides(self):
        from skill_eval.models.batch_models import CalibrationSummary
        from skill_eval.models.enums import CalibrationVerdict
        per_side = {
            "v1": [(1, 1, self._rep(CalibrationVerdict.TRUSTED))],
            "v2": [(1, 1, self._rep(CalibrationVerdict.HIGH_SCORE_SUSPECT, hard_violations=[self._hv()]))],
        }
        s = CalibrationSummary.from_per_side(per_side)
        assert s.sides["v1"].total == 1 and len(s.sides["v1"].high_score_suspects) == 0
        assert len(s.sides["v2"].high_score_suspects) == 1

    def test_calibration_summary_empty_side_is_none(self):
        from skill_eval.models.batch_models import CalibrationSummary
        s = CalibrationSummary.from_per_side({"with_skill": None, "without_skill": None})
        assert s.sides["with_skill"] is None


def test_benchmark_data_llm_token_usage_defaults_empty():
    from skill_eval.models import BenchmarkData, BenchmarkMetadata
    d = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s"))
    assert d.llm_token_usage == {}


def test_benchmark_data_llm_token_usage_roundtrip():
    from skill_eval.models import BenchmarkData, BenchmarkMetadata
    tu = {"scoring": {"input_tokens": 100, "output_tokens": 50, "total_cost_usd": 0.01, "calls": 3},
          "total": {"input_tokens": 200, "output_tokens": 80, "total_cost_usd": 0.02}}
    d = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s"), llm_token_usage=tu)
    js = d.model_dump_json()
    d2 = BenchmarkData.model_validate_json(js)
    assert d2.llm_token_usage == tu


def test_legacy_benchmark_json_without_llm_token_usage_loads():
    """旧 benchmark.json 无该字段 → {}(向后兼容)。"""
    from skill_eval.models import BenchmarkData
    legacy = '{"metadata":{"skill_name":"s"},"runs":[],"run_summary":{}}'
    d = BenchmarkData.model_validate_json(legacy)
    assert d.llm_token_usage == {}


def test_timing_data_grader_token_usage_field():
    from skill_eval.models.grading_models import TimingData
    t = TimingData()
    assert t.grader_token_usage == {}
    t2 = TimingData(total_tokens=5, grader_token_usage={"input_tokens": 9})
    assert t2.grader_token_usage == {"input_tokens": 9}


def test_timing_data_grader_duration_field():
    from skill_eval.models.grading_models import TimingData
    t = TimingData()
    assert t.grader_duration_seconds == 0.0
    t2 = TimingData(grader_duration_seconds=2.5)
    assert t2.grader_duration_seconds == 2.5
    # legacy:旧 json 无该字段 → 0.0
    assert TimingData.model_validate_json('{"total_tokens":5}').grader_duration_seconds == 0.0


def test_audit_report_duration_seconds_field():
    from skill_eval.models.audit_models import AuditReport
    r2 = AuditReport(skill_name="s", transcripts=[], summary={}, findings=[], duration_seconds=87.0)
    assert r2.duration_seconds == 87.0
    assert AuditReport.model_validate_json(r2.model_dump_json()).duration_seconds == 87.0
    # legacy:旧 report 无该字段 → 0.0(向后兼容)
    d = r2.model_dump(mode="json")
    d.pop("duration_seconds", None)
    assert AuditReport.model_validate(d).duration_seconds == 0.0


def test_benchmark_data_llm_duration_defaults_empty():
    from skill_eval.models import BenchmarkData, BenchmarkMetadata
    d = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s"))
    assert d.llm_duration_seconds == {}


def test_benchmark_data_llm_duration_roundtrip():
    from skill_eval.models import BenchmarkData, BenchmarkMetadata
    du = {"scoring": 1.5, "audit": 3.0, "executor": 0.5, "total": 10.0}
    d = BenchmarkData(metadata=BenchmarkMetadata(skill_name="s"), llm_duration_seconds=du)
    d2 = BenchmarkData.model_validate_json(d.model_dump_json())
    assert d2.llm_duration_seconds == du


# ---------------------------------------------------------------------------
# 候选①:EvalCase rationale + confidence(per-eval 溯源 + 把握度)
# ---------------------------------------------------------------------------

def test_eval_case_rationale_confidence_defaults():
    ec = EvalCase(id=1, prompt="x")
    assert ec.rationale == ""
    assert ec.confidence is None


def test_eval_case_rationale_confidence_roundtrip():
    ec = EvalCase(id=1, prompt="x", rationale="## 步骤 bullet 2", confidence=0.8)
    ec2 = EvalCase.model_validate_json(ec.model_dump_json())
    assert ec2.rationale == "## 步骤 bullet 2"
    assert abs(ec2.confidence - 0.8) < 1e-9


def test_legacy_eval_json_without_rationale_loads():
    """旧 evals.json 无 rationale/confidence → 空默认(向后兼容)。"""
    ec = EvalCase.model_validate_json('{"id":1,"prompt":"x"}')
    assert ec.rationale == "" and ec.confidence is None


# ---------------------------------------------------------------------------
# trigger-accuracy 探题:独立 trigger_probes 段(不进 evals,不背 scoring 空字段)
# ---------------------------------------------------------------------------

def test_trigger_probe_fields():
    from skill_eval.models.eval_models import TriggerProbe
    p = TriggerProbe(id=1, prompt="分析这份日志", expect_trigger=True, rationale="正中用途")
    assert p.expect_trigger is True
    assert p.rationale == "正中用途"


def test_evalset_trigger_probes_default_empty():
    es = EvalSet(skill_name="x", evals=[])
    assert es.trigger_probes == []


def test_evalset_backward_compat_old_json_no_trigger_probes():
    """旧 evals.json 无 trigger_probes key → 空 list(向后兼容)。"""
    old = {"skill_name": "x", "evals": [{"id": 1, "prompt": "p"}]}
    es = EvalSet.model_validate(old)
    assert es.trigger_probes == []
    assert es.evals[0].prompt == "p"


def test_evalset_roundtrip_with_trigger_probes():
    from skill_eval.models.eval_models import TriggerProbe
    es = EvalSet(skill_name="x", evals=[],
                 trigger_probes=[TriggerProbe(id=1, prompt="p", expect_trigger=False)])
    js = es.model_dump(mode="json")
    es2 = EvalSet.model_validate(js)
    assert es2.trigger_probes[0].expect_trigger is False


# ---------------------------------------------------------------------------
# trigger-accuracy 报告:TriggerReport / ProbeResult(写 trigger-report.json)
# ---------------------------------------------------------------------------

def test_trigger_report_roundtrip():
    from skill_eval.models.trigger_report import TriggerReport, ProbeResult
    r = TriggerReport(skill_name="x", description_hash="abc", runs=1,
                      recall=0.5, false_trigger_rate=0.0, n_should_trigger=2, n_should_not=1,
                      unresolved=0, token_usage={"total_tokens": 100},
                      probes=[ProbeResult(id=1, expect_trigger=True, trigger_rate=1.0, verdict="PASS")])
    r2 = TriggerReport.model_validate_json(r.model_dump_json())
    assert r2.recall == 0.5 and r2.description_hash == "abc"
    assert r2.probes[0].verdict == "PASS" and r2.probes[0].transcript_path == ""


def test_trigger_report_optional_rates_can_be_none():
    """recall / false_trigger_rate 可为 None(某方向无探题 → N/A)。"""
    from skill_eval.models.trigger_report import TriggerReport
    r = TriggerReport(skill_name="x", description_hash="h", runs=1,
                      recall=None, false_trigger_rate=None,
                      n_should_trigger=0, n_should_not=0, probes=[])
    r2 = TriggerReport.model_validate_json(r.model_dump_json())
    assert r2.recall is None and r2.false_trigger_rate is None


def test_trigger_report_description_prompt_suggestions_roundtrip():
    """description / prompt / suggestions 新字段 roundtrip + 旧报告(无这些字段)兜默认。"""
    from skill_eval.models.trigger_report import TriggerReport, ProbeResult, Suggestion
    r = TriggerReport(
        skill_name="x", description="does X for logs", description_hash="abc", runs=2,
        recall=0.5, false_trigger_rate=0.0, n_should_trigger=2, n_should_not=1,
        suggestions=[
            Suggestion(category="recall", source="rule", message="补关键词", probe_ids=[1, 3]),
            Suggestion(category="llm_rewrite", source="llm", message="改写草案…"),
        ],
        probes=[ProbeResult(id=1, expect_trigger=True, prompt="分析日志", trigger_rate=0.0, verdict="FAIL")],
    )
    r2 = TriggerReport.model_validate_json(r.model_dump_json())
    assert r2.description == "does X for logs"
    assert r2.probes[0].prompt == "分析日志"
    assert len(r2.suggestions) == 2
    assert r2.suggestions[0].category == "recall" and r2.suggestions[0].probe_ids == [1, 3]
    assert r2.suggestions[1].source == "llm"


def test_trigger_report_legacy_json_without_new_fields_loads():
    """旧 trigger-report.json(无 description/prompt/suggestions)→ 兜默认,不炸。"""
    from skill_eval.models.trigger_report import TriggerReport
    legacy = {
        "skill_name": "x", "description_hash": "abc", "runs": 1,
        "recall": 1.0, "false_trigger_rate": None, "n_should_trigger": 1, "n_should_not": 0,
        "unresolved": 0, "token_usage": {}, "probes": [
            {"id": 1, "expect_trigger": True, "trigger_rate": 1.0, "verdict": "PASS", "transcript_path": ""},
        ],
    }
    r = TriggerReport.model_validate(legacy)
    assert r.description == "" and r.suggestions == [] and r.probes[0].prompt == ""


def test_trigger_report_duration_seconds_roundtrip():
    """duration_seconds(cc spawn 总墙钟)roundtrip + 旧报告(无此字段)兜默认 0.0。"""
    from skill_eval.models.trigger_report import TriggerReport, ProbeResult
    r = TriggerReport(skill_name="x", description_hash="h", runs=1,
                      recall=1.0, false_trigger_rate=None, n_should_trigger=1, n_should_not=0,
                      duration_seconds=12.5,
                      probes=[ProbeResult(id=1, expect_trigger=True, trigger_rate=1.0, verdict="PASS")])
    assert r.duration_seconds == 12.5
    assert TriggerReport.model_validate_json(r.model_dump_json()).duration_seconds == 12.5
    d = json.loads(r.model_dump_json())
    d.pop("duration_seconds", None)
    assert TriggerReport.model_validate(d).duration_seconds == 0.0   # 旧报告无此字段 → 默认 0.0


def test_probe_result_tokens_and_duration_roundtrip():
    """ProbeResult per-probe 消耗(tokens + duration_seconds)roundtrip + 旧报告兜默认。"""
    from skill_eval.models.trigger_report import TriggerReport, ProbeResult
    r = TriggerReport(
        skill_name="x", description_hash="h", runs=2,
        recall=1.0, false_trigger_rate=None, n_should_trigger=1, n_should_not=0,
        probes=[ProbeResult(id=1, expect_trigger=True, trigger_rate=1.0, verdict="PASS",
                            tokens=842, duration_seconds=4.5)],
    )
    r2 = TriggerReport.model_validate_json(r.model_dump_json())
    assert r2.probes[0].tokens == 842 and r2.probes[0].duration_seconds == 4.5
    legacy = json.loads(r.model_dump_json())
    for p in legacy["probes"]:
        p.pop("tokens", None)
        p.pop("duration_seconds", None)
    r3 = TriggerReport.model_validate(legacy)
    assert r3.probes[0].tokens == 0 and r3.probes[0].duration_seconds == 0.0   # 旧 probe 无字段 → 默认
