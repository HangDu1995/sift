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


SAMPLE_BODY_CN = """\
# 工作流

1. 使用 Read 工具读取输入文件
2. 运行 bash 命令检查环境
3. 调用 scripts/validate.py 验证结果

## 规则

- 严禁捏造数据或编造缺失值
- 禁止跳过验证步骤
- 必须使用 UTF-8 编码

## 反捏造

禁止捏造数据。不得编造数值。严禁虚构内容。

## 格式

```python
import json
json.dump(data, f, indent=2)
```

参考 references/guide.md 了解最佳实践。
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


class TestInstructionExtractorChinese:
    """中文 SKILL.md 提取(P0-1:补中文关键词后,约束 / 工具 / 反捏造不再漏判)."""

    def test_extracts_constraints_cn(self):
        ext = InstructionExtractor()
        constraints = [c for c in ext.extract(SAMPLE_BODY_CN) if c.source == InstructionSource.CONSTRAINT]
        assert len(constraints) >= 3
        texts = "".join(c.instruction_text for c in constraints)
        assert "严禁" in texts
        assert "禁止" in texts
        assert "必须" in texts

    def test_extracts_tool_requirements_cn(self):
        ext = InstructionExtractor()
        tools = [c for c in ext.extract(SAMPLE_BODY_CN) if c.source == InstructionSource.TOOL_REQUIREMENT]
        assert len(tools) > 0
        names = [c.params.get("tool_name") for c in tools]
        assert "bash" in names
        assert "scripts" in names  # 调用 scripts/validate.py -> tool_name=scripts
        for t in tools:
            assert t.check_method == CheckMethod.TOOL_CALL_CHECK

    def test_extracts_anti_fabrication_cn(self):
        ext = InstructionExtractor()
        af = [c for c in ext.extract(SAMPLE_BODY_CN) if c.source == InstructionSource.ANTI_FABRICATION]
        assert len(af) > 0
        texts = "".join(c.instruction_text for c in af)
        assert "捏造" in texts or "编造" in texts or "虚构" in texts

    def test_english_not_regressed(self):
        """英文 baseline 不回归:'using' 不应被拆成 use+ing。"""
        ext = InstructionExtractor()
        tools = [c for c in ext.extract(SAMPLE_BODY) if c.source == InstructionSource.TOOL_REQUIREMENT]
        names = [c.params.get("tool_name") for c in tools]
        assert "ing" not in names


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

    def test_no_dual_evaluation_agreement_rate_is_none(self):
        """P2-3: 无 prog/llm 双判时 agreement_rate=None(非假 0.0),且给无双判提示。"""
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="OK output", source="programmatic"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        report = cal.calibrate(grading)
        assert report.agreement_rate is None, "无双判时 agreement_rate 应为 None,不是 0.0"
        assert report.both_evaluated == 0
        assert any("双判" in r for r in report.recommendations), "应给无双判提示"

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


class TestConstraintFiltering:
    """P0-1b: CONSTRAINT 候选 LLM 精筛 —— '必须'类过 LLM 去假阳性,禁止性词直接保留。"""

    @staticmethod
    def _constraint(text: str) -> InstructionCheck:
        return InstructionCheck(
            source=InstructionSource.CONSTRAINT,
            instruction_text=text,
            section="Constraint",
            priority=InstructionPriority.REQUIRED,
            check_method=CheckMethod.LLM_JUDGE,
        )

    def test_prohibitive_kept_without_llm(self, mocker):
        """禁止性词(禁止/严禁/不得…)确定性高,不过 LLM 直接保留。"""
        spy = mocker.patch("skill_eval.compliance.checker.call_claude")
        checker = ComplianceChecker()
        instructions = [
            self._constraint("禁止跳过验证步骤"),
            self._constraint("严禁捏造数据"),
        ]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert len(filtered) == 2
        assert n == 0
        spy.assert_not_called()

    def test_must_false_positive_filtered(self, mocker):
        """'必须'路由提示/描述语经 LLM 判非可遵循 → 剔除。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='[{"index": 0, "applicable": false}]',
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("（⚠️ 必须先读） / [算法](references/x.md)")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert len(filtered) == 0
        assert n == 1

    def test_must_real_constraint_kept(self, mocker):
        """'必须'真约束(必须使用 UTF-8)经 LLM 判可遵循 → 保留。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='[{"index": 0, "applicable": true}]',
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须使用 UTF-8 编码")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert len(filtered) == 1
        assert n == 0

    def test_llm_unavailable_keeps_all(self, mocker):
        """LLM 不可用(RuntimeError)时保守保留全部,不误剔。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            side_effect=RuntimeError("timeout"),
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须先读 references/x.md")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert len(filtered) == 1
        assert n == 0

    def test_prohibitive_and_must_mixed(self, mocker):
        """混合:禁止性保留不过 LLM,'必须'过 LLM;LLM 未提的 index 默认保留。"""
        spy = mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='[{"index": 0, "applicable": false}]',
        )
        checker = ComplianceChecker()
        instructions = [
            self._constraint("禁止跳过"),
            self._constraint("必须先读 x.md"),
            self._constraint("必须使用 UTF-8"),
        ]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert n == 1  # 只剔"必须先读"
        assert len(filtered) == 2
        texts = [i.instruction_text for i in filtered]
        assert "禁止跳过" in texts
        assert "必须使用 UTF-8" in texts
        spy.assert_called_once()  # 禁止性不过 LLM,只对 2 条"必须"候选调一次
