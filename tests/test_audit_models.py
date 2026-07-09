"""audit 命令输出模型的单测(TDD:先写失败测试,再实现 audit_models.py)。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from skill_eval.models import (
    AuditCategory,
    AuditFinding,
    AuditReport,
    AuditVerdict,
)


class TestAuditEnums:
    """枚举值正确性。"""

    def test_verdict_values(self) -> None:
        assert AuditVerdict.PASS.value == "PASS"
        assert AuditVerdict.FAIL.value == "FAIL"
        assert AuditVerdict.NA.value == "N/A"

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
            "category": AuditCategory.PROHIBITION,
            "evidence": "transcript 中出现了被禁止的调用",
            "confidence": 0.8,
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
        assert f.confidence == pytest.approx(0.8)
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


class TestAuditReport:
    """AuditReport 构造 + 默认值 + 序列化往返。"""

    def _make_finding(self, **overrides: object) -> AuditFinding:
        base: dict[str, object] = {
            "instruction_text": "必须调用 Read",
            "source": "TOOL_REQUIREMENT",
            "verdict": AuditVerdict.PASS,
            "category": AuditCategory.CONDITIONAL,
            "evidence": "transcript 中存在 Read 调用",
            "confidence": 0.9,
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
                category=AuditCategory.PROHIBITION,
                evidence="无法判定",
                confidence=0.3,
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
        assert f0.derived_from_transcript is False

        assert f1.verdict is AuditVerdict.NA
        assert f1.category is AuditCategory.PROHIBITION
        assert f1.source == "ANTI_FABRICATION"
        assert f1.confidence == pytest.approx(0.3)
        assert f1.derived_from_transcript is True

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
