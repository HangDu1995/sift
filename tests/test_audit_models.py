"""audit 命令输出模型的单测(TDD:先写失败测试,再实现 audit_models.py)。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sift.models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)


class TestAuditEnums:
    """枚举值正确性。"""

    def test_verdict_values(self) -> None:
        assert AuditVerdict.PASS.value == "PASS"
        assert AuditVerdict.FAIL.value == "FAIL"
        assert AuditVerdict.NA.value == "N/A"
        assert AuditVerdict.UNRESOLVED.value == "UNRESOLVED"  # 没判成
        assert AuditVerdict.INDETERMINATE.value == "INDETERMINATE"  # 判不准

    def test_method_values(self) -> None:
        assert AuditMethod.PROGRAMMATIC.value == "PROGRAMMATIC"
        assert AuditMethod.LLM.value == "LLM"

    def test_verdict_is_str_enum(self) -> None:
        # str, Enum -> 直接当字符串用
        assert AuditVerdict.PASS == "PASS"
        assert AuditVerdict.NA == "N/A"

    def test_category_values(self) -> None:
        assert AuditCategory.CONDITIONAL.value == "conditional"
        assert AuditCategory.PROHIBITION.value == "prohibition"
        assert AuditCategory.STEP.value == "step"
        assert AuditCategory.OUTPUT.value == "output"

    def test_category_is_str_enum(self) -> None:
        assert AuditCategory.CONDITIONAL == "conditional"
        assert AuditCategory.OUTPUT == "output"


class TestAuditFinding:
    """AuditFinding 构造 + 默认值。"""

    def _make(self, **overrides: object) -> AuditFinding:
        base: dict[str, object] = {
            "instruction_text": "禁止使用 constraint X",
            "source": "CONSTRAINT",
            "verdict": AuditVerdict.FAIL,
            "method": AuditMethod.LLM,
            "category": AuditCategory.PROHIBITION,
            "evidence": "transcript 中出现了被禁止的调用",
            "transcript": "run-1.jsonl",
        }
        base.update(overrides)
        return AuditFinding(**base)  # type: ignore[arg-type]

    def test_required_fields_construct(self) -> None:
        f = self._make()
        assert f.instruction_text == "禁止使用 constraint X"
        assert f.source == "CONSTRAINT"
        assert f.verdict is AuditVerdict.FAIL
        assert f.category is AuditCategory.PROHIBITION
        assert f.method is AuditMethod.LLM
        assert f.transcript == "run-1.jsonl"

    def test_derived_from_transcript_defaults_false(self) -> None:
        f = self._make()
        assert f.derived_from_transcript is False

    def test_derived_from_transcript_settable(self) -> None:
        f = self._make(derived_from_transcript=True)
        assert f.derived_from_transcript is True

    def test_verdict_accepts_raw_string(self) -> None:
        # str, Enum -> 也能用裸字符串构造(pydantic 会 coerce)
        f = self._make(verdict="PASS", category="step")
        assert f.verdict is AuditVerdict.PASS
        assert f.category is AuditCategory.STEP

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            AuditFinding(  # 缺 verdict / category / 等
                instruction_text="x",
                source="CONSTRAINT",
            )

    def test_unresolved_cause_defaults_none(self) -> None:
        """新字段默认 None,向后兼容(旧 JSON / 非 UNRESOLVED finding)。"""
        f = AuditFinding(
            instruction_text="x", source="CONSTRAINT",
            verdict=AuditVerdict.PASS, method=AuditMethod.LLM,
            category=AuditCategory.PROHIBITION, evidence="e", transcript="t.jsonl",
        )
        assert f.unresolved_cause is None

    def test_unresolved_cause_settable(self) -> None:
        f = AuditFinding(
            instruction_text="x", source="CONSTRAINT",
            verdict=AuditVerdict.UNRESOLVED, method=AuditMethod.LLM,
            category=AuditCategory.PROHIBITION, evidence="e", transcript="t.jsonl",
            unresolved_cause="timeout",
        )
        assert f.unresolved_cause == "timeout"


class TestAuditReportUnresolvedBreakdown:
    def test_default_empty(self) -> None:
        r = AuditReport(skill_name="s", transcripts=[], summary={}, findings=[])
        assert r.unresolved_breakdown == {}

    def test_legacy_json_no_field_loads_as_empty(self) -> None:
        # 旧 report JSON 无 unresolved_breakdown 字段 → 默认 {}
        legacy = {"skill_name": "s", "transcripts": [], "summary": {},
                  "findings": []}
        r = AuditReport.model_validate(legacy)
        assert r.unresolved_breakdown == {}


class TestAuditReport:
    """AuditReport 构造 + 默认值 + 序列化往返。"""

    def _make_finding(self, **overrides: object) -> AuditFinding:
        base: dict[str, object] = {
            "instruction_text": "必须调用 Read",
            "source": "TOOL_REQUIREMENT",
            "verdict": AuditVerdict.PASS,
            "method": AuditMethod.PROGRAMMATIC,
            "category": AuditCategory.CONDITIONAL,
            "evidence": "transcript 中存在 Read 调用",
            "transcript": "run-1.jsonl",
        }
        base.update(overrides)
        return AuditFinding(**base)  # type: ignore[arg-type]

    def test_findings_required(self) -> None:
        with pytest.raises(ValidationError):
            AuditReport(  # 缺 findings
                skill_name="demo",
                transcripts=["run-1.jsonl"],
                summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
            )

    def test_warnings_default_empty(self) -> None:
        report = AuditReport(
            skill_name="demo",
            transcripts=["run-1.jsonl"],
            summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
            findings=[self._make_finding()],
        )
        assert report.warnings == []

    def test_summary_dict_holds_counts(self) -> None:
        summary = {"total": 3, "pass": 1, "fail": 1, "na": 1}
        report = AuditReport(
            skill_name="demo",
            transcripts=["a.jsonl", "b.jsonl"],
            summary=summary,
            findings=[self._make_finding()],
        )
        assert report.summary == summary
        assert report.summary["na"] == 1
        assert report.summary["total"] == 3

    def test_roundtrip_preserves_fields(self) -> None:
        findings = [
            self._make_finding(),
            self._make_finding(
                instruction_text="不许编造",
                source="ANTI_FABRICATION",
                verdict=AuditVerdict.NA,
                method=AuditMethod.LLM,
                category=AuditCategory.PROHIBITION,
                evidence="无法判定",
                transcript="run-2.jsonl",
                derived_from_transcript=True,
            ),
        ]
        report = AuditReport(
            skill_name="demo-skill",
            transcripts=["run-1.jsonl", "run-2.jsonl"],
            summary={"total": 2, "pass": 1, "fail": 0, "na": 1},
            findings=findings,
            warnings=["某个指令反推不确定"],
        )

        js = report.model_dump_json()
        restored = AuditReport.model_validate_json(js)

        assert restored.skill_name == "demo-skill"
        assert restored.transcripts == ["run-1.jsonl", "run-2.jsonl"]
        assert restored.summary == {"total": 2, "pass": 1, "fail": 0, "na": 1}
        assert restored.warnings == ["某个指令反推不确定"]
        assert len(restored.findings) == 2

        f0, f1 = restored.findings
        assert f0.verdict is AuditVerdict.PASS
        assert f0.category is AuditCategory.CONDITIONAL
        assert f0.method is AuditMethod.PROGRAMMATIC
        assert f0.derived_from_transcript is False

        assert f1.verdict is AuditVerdict.NA
        assert f1.category is AuditCategory.PROHIBITION
        assert f1.source == "ANTI_FABRICATION"
        assert f1.method is AuditMethod.LLM
        assert f1.derived_from_transcript is True

    def test_legacy_unclear_folds_to_unresolved(self) -> None:
        """旧 JSON(verdict=UNCLEAR + confidence 浮点)经 legacy validator:
        UNCLEAR → UNRESOLVED(旧 UNCLEAR 实际只装"没判成");confidence → method(>=1.0 程序,否则 LLM)。
        使旧 run 产物仍可加载,不崩。"""
        legacy = {
            "instruction_text": "x", "source": "CONSTRAINT",
            "verdict": "UNCLEAR", "category": "prohibition",
            "evidence": "e", "confidence": 0.3, "transcript": "t.jsonl",
        }
        f = AuditFinding.model_validate(legacy)
        assert f.verdict is AuditVerdict.UNRESOLVED
        assert f.method is AuditMethod.LLM  # confidence 0.3 < 1.0 → LLM

        # confidence 1.0(旧程序判定)→ PROGRAMMATIC
        prog = {**legacy, "verdict": "PASS", "confidence": 1.0}
        f2 = AuditFinding.model_validate(prog)
        assert f2.method is AuditMethod.PROGRAMMATIC

    def test_roundtrip_na_enum_value(self) -> None:
        # N/A 这个值含斜杠,确保 JSON 往返不丢
        f = self._make_finding(verdict=AuditVerdict.NA)
        report = AuditReport(
            skill_name="x",
            transcripts=["t.jsonl"],
            summary={"total": 1, "pass": 0, "fail": 0, "na": 1},
            findings=[f],
        )
        restored = AuditReport.model_validate_json(report.model_dump_json())
        assert restored.findings[0].verdict is AuditVerdict.NA
        assert restored.findings[0].verdict.value == "N/A"

    def test_token_usage_defaults_empty(self) -> None:
        # 不传 → 默认空 dict(向后兼容旧 report / 无 LLM 调用的 audit)。
        report = AuditReport(
            skill_name="demo",
            transcripts=["run-1.jsonl"],
            summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
            findings=[self._make_finding()],
        )
        assert report.token_usage == {}

    def test_token_usage_roundtrip(self) -> None:
        # token 用量(call_claude json 模式抓的 usage+cost)经 JSON 往返不丢。
        tu = {
            "input_tokens": 1234, "output_tokens": 56,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7,
            "total_cost_usd": 0.0234, "calls": 3,
        }
        report = AuditReport(
            skill_name="demo",
            transcripts=["run-1.jsonl"],
            summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
            findings=[self._make_finding()],
            token_usage=tu,
        )
        restored = AuditReport.model_validate_json(report.model_dump_json())
        assert restored.token_usage == tu

    def test_legacy_json_without_token_usage_loads(self) -> None:
        # 旧 report(本期之前)没有 token_usage 字段 → 加载默认 {},不崩。
        legacy = {
            "skill_name": "demo",
            "transcripts": ["run-1.jsonl"],
            "summary": {"total": 1, "pass": 1, "fail": 0, "na": 0},
            "findings": [
                {
                    "instruction_text": "必须调用 Read", "source": "TOOL_REQUIREMENT",
                    "verdict": "PASS", "method": "PROGRAMMATIC", "category": "conditional",
                    "evidence": "e", "transcript": "run-1.jsonl",
                }
            ],
        }
        restored = AuditReport.model_validate_json(json.dumps(legacy))
        assert restored.token_usage == {}
