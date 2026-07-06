"""Tests for compliance engine and calibration checker."""

from __future__ import annotations

from pathlib import Path

import pytest

from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.compliance.checker import (
    ComplianceChecker,
    _APPLICABILITY_VOTES,
    _extract_first_balanced,
    clear_applicability_cache,
)
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
        # P0-1d: REFERENCE_USAGE 走 TOOL_CALL_CHECK 查真 Read/调用(非 FILE_PRESENCE 恒真),
        # extractor 须发 script_path 供 _check_tool_call 匹配 tool_trace 入参
        for r in refs:
            assert r.check_method == CheckMethod.TOOL_CALL_CHECK
            assert r.params.get("script_path"), "REFERENCE_USAGE 须带 script_path"

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

    def test_reference_usage_tool_call_check(self, tmp_path: Path):
        """P0-1d: REFERENCE_USAGE 走 TOOL_CALL_CHECK —— tool_trace 里有 Read(ref) → followed;
        没读该 ref → not followed。替代旧 FILE_PRESENCE(P0-2 拷文件后恒真、无意义)。"""
        checker = ComplianceChecker()
        inst = InstructionCheck(
            source=InstructionSource.REFERENCE_USAGE,
            instruction_text="Use reference file: references/guide.md",
            section="References",
            priority=InstructionPriority.RECOMMENDED,
            check_method=CheckMethod.TOOL_CALL_CHECK,
            params={"script_path": "references/guide.md"},
        )
        # 读过该 ref → followed
        trace_read = [{"name": "Read", "input_summary": "reading references/guide.md"}]
        item = checker._check_instruction(
            inst, transcript="", output_dir=tmp_path, model=None, timeout=5, tool_trace=trace_read
        )
        assert item.followed is True
        assert item.source == "programmatic"
        # 没读该 ref(读了别的)→ not followed
        trace_other = [{"name": "Read", "input_summary": "reading some_other.md"}]
        item2 = checker._check_instruction(
            inst, transcript="", output_dir=tmp_path, model=None, timeout=5, tool_trace=trace_other
        )
        assert item2.followed is False
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

    @pytest.fixture(autouse=True)
    def _clear_applicability_cache(self):
        """P0-1c: 模块级缓存跨测试会污染,每个测试前清空。"""
        clear_applicability_cache()
        yield

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
        # P0-1c: self-consistency 把 2 条"必须"候选打包,判 N 次(N=_APPLICABILITY_VOTES)
        assert spy.call_count == _APPLICABILITY_VOTES

    def test_self_consistency_majority_filters(self, mocker):
        """P0-1c: N 次采样多数判非可遵循 → 剔除(单次抽签可能错,多数票稳)。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            side_effect=[
                '[{"index": 0, "applicable": false}]',
                '[{"index": 0, "applicable": false}]',
                '[{"index": 0, "applicable": true}]',  # 仅 1 票说可遵循
            ],
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须先读 references/x.md")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert n == 1  # 多数(2/3)判非可遵循 → 剔除
        assert len(filtered) == 0

    def test_self_consistency_majority_keeps(self, mocker):
        """P0-1c: N 次采样多数判可遵循 → 保留。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            side_effect=[
                '[{"index": 0, "applicable": true}]',
                '[{"index": 0, "applicable": true}]',
                '[{"index": 0, "applicable": false}]',  # 仅 1 票说非可遵循
            ],
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须使用 UTF-8 编码")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert n == 0  # 多数(2/3)判可遵循 → 保留
        assert len(filtered) == 1

    def test_cache_hit_skips_recompute(self, mocker):
        """P0-1c: 同候选二次过滤命中缓存,call_claude 不再调(filtered_count 确定一致)。"""
        spy = mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='[{"index": 0, "applicable": false}]',
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须先读 references/cache.md")]
        f1, n1 = checker._filter_constraint_candidates(instructions, model=None)
        calls_after_first = spy.call_count
        assert calls_after_first == _APPLICABILITY_VOTES  # 首次判 N 次
        f2, n2 = checker._filter_constraint_candidates(instructions, model=None)
        assert spy.call_count == calls_after_first  # 二次命中缓存,不再调
        assert (f1, n1) == (f2, n2)  # 结果一致 → filtered_count 跨 run 确定

    def test_too_few_samples_keeps_all(self, mocker):
        """P0-1c: 成功样本 < 半数请求 → 保守全保留(不据薄证据剔)。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            side_effect=[
                RuntimeError("down"),
                '[{"index": 0, "applicable": false}]',
                RuntimeError("down"),
            ],
        )
        checker = ComplianceChecker()
        instructions = [self._constraint("必须先读 references/x.md")]
        filtered, n = checker._filter_constraint_candidates(instructions, model=None)
        assert n == 0  # 仅 1/3 成功,太薄 → 保守保留
        assert len(filtered) == 1


class TestParseRobustness:
    """贪婪正则 bug 回归(P-fix):LLM 在 JSON 后用 [N]/[0]-[4] 引用条目时,
    旧贪婪正则 \\[[\\s\\S]*\\] 吞到文本末个 ] → json.loads 失败 → 默认全保留(适用性)/
    全 False(遵循)。这被误判成"LLM 漂移"(探针 10/10 证实实为 parser bug)。新平衡
    提取器须按括号深度配平 + 跳过字符串内括号,正确解析。"""

    def test_extract_balanced_arrays(self):
        assert _extract_first_balanced("[1, 2, 3]") == "[1, 2, 3]"
        # evidence 字符串里的 [0] 不算结构括号
        assert _extract_first_balanced('[{"e": "[0] 处"}]') == '[{"e": "[0] 处"}]'
        # markdown 围栏
        assert _extract_first_balanced("```json\n[1, 2]\n```") == "[1, 2]"
        # 嵌套数组
        assert _extract_first_balanced("[[1, 2], [3]]") == "[[1, 2], [3]]"

    def test_extract_balanced_objects(self):
        assert _extract_first_balanced('{"a": 1}', "{", "}") == '{"a": 1}'
        # evidence 含 } 须不被截断(旧 [^}]+ 会截在首个 })
        assert _extract_first_balanced('{"e": "dict {k: v}"}', "{", "}") == '{"e": "dict {k: v}"}'

    def test_extract_balanced_edge_cases(self):
        assert _extract_first_balanced("no array here") is None
        assert _extract_first_balanced("[1, 2,") is None  # 不平衡
        assert _extract_first_balanced("") is None
        # 转义引号不破坏字符串边界
        assert _extract_first_balanced(r'["a\"b]"]') == r'["a\"b]"]'

    def test_applicability_bracket_reasoning_not_default_keep(self):
        """GLM 返回 JSON + reasoning 含 [0]-[1] 引用 → 须解析为实际值(全 false),
        不是解析失败回退的全保留。这是 §39 [6,6,0] 漂移的真因。"""
        text = (
            '[{"index": 0, "applicable": false},'
            ' {"index": 1, "applicable": false}]\n\n'
            "判定依据:\n- [0]-[1] 均为表格注记「(⚠️ 必须先读)」,非可遵循。"
        )
        mask = ComplianceChecker._parse_applicability(text, 2)
        assert mask == [False, False]  # 旧贪婪正则:FAIL → 默认 [True, True]

    def test_batch_bracket_evidence_not_default_false(self):
        """batch evidence 含 [0] 引用 → 须正确解析,不退到默认全 False。"""
        insts = [
            InstructionCheck(
                source=InstructionSource.CONSTRAINT, instruction_text="t0", section="S",
                priority=InstructionPriority.REQUIRED, check_method=CheckMethod.LLM_JUDGE,
            ),
            InstructionCheck(
                source=InstructionSource.CONSTRAINT, instruction_text="t1", section="S",
                priority=InstructionPriority.REQUIRED, check_method=CheckMethod.LLM_JUDGE,
            ),
        ]
        text = (
            '[{"index": 0, "followed": true, "evidence": "见 [0] 处确认", "confidence": 0.9},'
            ' {"index": 1, "followed": false, "evidence": "未提及", "confidence": 0.8}]'
        )
        items = ComplianceChecker._parse_batch_result(text, insts)
        assert items[0].followed is True
        assert items[1].followed is False

    def test_single_brace_in_evidence(self, mocker):
        """单条判定 evidence 含 } → 须正确解析(旧 [^}]+ 在首个 } 截断 → 解析失败)。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='{"followed": true, "evidence": "dict {k: v} ok", "confidence": 0.9}',
        )
        checker = ComplianceChecker()
        inst = InstructionCheck(
            source=InstructionSource.CONSTRAINT, instruction_text="t", section="S",
            priority=InstructionPriority.REQUIRED, check_method=CheckMethod.LLM_JUDGE,
        )
        item = checker._check_llm_single(inst, "transcript", None, 5)
        assert item.followed is True
        assert "dict {k: v} ok" in item.evidence
