"""Tests for compliance engine and calibration checker."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.compliance.checker import ComplianceChecker
from skill_eval.calibration.calibrator import CalibrationChecker
from skill_eval.models import (
    CheckMethod,
    ComplianceReport,
    ComplianceResultItem,
    ExpectationResult,
    GradingResult,
    GradingSummary,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
    ConfidenceGrade,
)


SAMPLE_BODY = """\
# Workflow

1. Read the input files using the Read tool
2. Analyze the data structure
3. Create the transform script using scripts/validate.py
4. Run validation with `python scripts/validate.py`

## Rules

- NEVER fabricate data or invent missing values
- ALWAYS use UTF-8 encoding
- DO NOT skip the validation step

## Anti-Fabrication

Never fabricate data. Do not invent values. No hallucination of content.

## Format

```python
import json
json.dump(data, f, indent=2)
```

See references/guide.md for best practices.
"""


class TestInstructionExtractor:
    def test_extracts_heading_steps(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        heading_steps = [c for c in checks if c.source == InstructionSource.HEADING_STEP]
        assert len(heading_steps) > 0

    def test_extracts_tool_requirements(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        tool_reqs = [c for c in checks if c.source == InstructionSource.TOOL_REQUIREMENT]
        assert len(tool_reqs) > 0
        for tr in tool_reqs:
            assert tr.check_method == CheckMethod.TOOL_CALL_CHECK
            assert "tool_name" in tr.params

    def test_extracts_constraints(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        constraints = [c for c in checks if c.source == InstructionSource.CONSTRAINT]
        assert len(constraints) >= 1
        texts = " ".join(c.instruction_text for c in constraints)
        assert "NEVER" in texts or "DO NOT" in texts

    def test_extracts_anti_fabrication(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        af = [c for c in checks if c.source == InstructionSource.ANTI_FABRICATION]
        assert len(af) > 0

    def test_extracts_output_format(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        fmt = [c for c in checks if c.source == InstructionSource.OUTPUT_FORMAT]
        assert len(fmt) > 0

    def test_extracts_reference_usage(self):
        ext = InstructionExtractor()
        checks = ext.extract(SAMPLE_BODY)
        refs = [c for c in checks if c.source == InstructionSource.REFERENCE_USAGE]
        assert len(refs) > 0
        assert any("guide.md" in c.instruction_text for c in refs)

    def test_empty_body(self):
        ext = InstructionExtractor()
        checks = ext.extract("")
        assert checks == []


class TestComplianceChecker:
    def test_check_tool_call_present(self):
        checker = ComplianceChecker()
        instructions = [
            InstructionCheck(
                source=InstructionSource.TOOL_REQUIREMENT,
                instruction_text="Use the Read tool",
                section="Workflow",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"tool_name": "Read"},
            ),
        ]
        report = checker.check(
            instructions=instructions,
            transcript="I will use the Read tool to open the file",
            output_dir=Path("/tmp"),
        )
        assert report.total_instructions == 1
        assert report.followed_count == 1
        assert report.compliance_rate == 1.0

    def test_check_tool_call_missing(self):
        checker = ComplianceChecker()
        instructions = [
            InstructionCheck(
                source=InstructionSource.TOOL_REQUIREMENT,
                instruction_text="Use the Bash tool",
                section="Workflow",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"tool_name": "Bash"},
            ),
        ]
        report = checker.check(
            instructions=instructions,
            transcript="I just wrote the file directly",
            output_dir=Path("/tmp"),
        )
        assert report.followed_count == 0

    def test_check_file_presence(self, tmp_path: Path):
        (tmp_path / "output.json").write_text("{}", encoding="utf-8")
        checker = ComplianceChecker()
        instructions = [
            InstructionCheck(
                source=InstructionSource.REFERENCE_USAGE,
                instruction_text="Output file exists",
                section="Output",
                priority=InstructionPriority.RECOMMENDED,
                check_method=CheckMethod.FILE_PRESENCE,
                params={"path": "output.json"},
            ),
        ]
        report = checker.check(
            instructions=instructions,
            transcript="",
            output_dir=tmp_path,
        )
        assert report.followed_count == 1

    def test_compliance_rate(self):
        checker = ComplianceChecker()
        instructions = [
            InstructionCheck(
                source=InstructionSource.TOOL_REQUIREMENT,
                instruction_text="Use Read",
                section="S1",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"tool_name": "Read"},
            ),
            InstructionCheck(
                source=InstructionSource.TOOL_REQUIREMENT,
                instruction_text="Use Write",
                section="S2",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"tool_name": "Write"},
            ),
        ]
        report = checker.check(
            instructions=instructions,
            transcript="I used the Read tool but not the other one",
            output_dir=Path("/tmp"),
        )
        assert report.compliance_rate == 0.5


class TestCalibrationChecker:
    def test_high_confidence(self):
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="Strong evidence from transcript", source="programmatic"),
                ExpectationResult(text="e1", passed=True, evidence="Strong evidence from analysis", source="llm"),
                ExpectationResult(text="e2", passed=True, evidence="Verified in output file", source="programmatic"),
                ExpectationResult(text="e2", passed=True, evidence="Output matches expected format", source="llm"),
            ],
            summary=GradingSummary(passed=4, failed=0, total=4, pass_rate=1.0),
        )
        report = cal.calibrate(grading)
        assert report.grade in (ConfidenceGrade.HIGH, ConfidenceGrade.MEDIUM)
        assert report.agreement_rate == 1.0

    def test_low_agreement(self):
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="Found in output", source="programmatic"),
                ExpectationResult(text="e1", passed=False, evidence="Not semantically correct", source="llm"),
            ],
            summary=GradingSummary(passed=1, failed=1, total=2, pass_rate=0.5),
        )
        report = cal.calibrate(grading)
        assert len(report.disagreements) == 1
        assert report.agreement_rate == 0.0

    def test_with_compliance(self):
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="OK", source="programmatic"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        compliance = ComplianceReport(
            skill_name="test",
            total_instructions=2,
            followed_count=1,
            compliance_rate=0.5,
            checks=[],
        )
        report = cal.calibrate(grading, compliance)
        assert any(s.signal_type == "COMPLIANCE_CONTRADICTS_SCORING" for s in report.confidence_signals) or report.overall_confidence < 0.7

    def test_grade_thresholds(self):
        cal = CalibrationChecker()
        assert cal._grade(0.9) == ConfidenceGrade.HIGH
        assert cal._grade(0.7) == ConfidenceGrade.MEDIUM
        assert cal._grade(0.5) == ConfidenceGrade.LOW
        assert cal._grade(0.3) == ConfidenceGrade.UNRELIABLE

    def test_recommendations_generated(self):
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="Short", source="programmatic"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        report = cal.calibrate(grading)
        assert isinstance(report.recommendations, list)
