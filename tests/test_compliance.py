"""Tests for compliance engine and calibration checker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.compliance.checker import (
    ComplianceChecker,
    _extract_first_balanced,
    extract_first_balanced,
    match_tool_call,
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
    ToolCallRecord,
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

    def test_tool_requirement_filters_stopword_garbage(self):
        """停用词碎片(use when / run concurrently / use of)不当 TOOL_REQUIREMENT 抽。

        回归:audit 实测 "use when" 被抽成 tool_name="when",LLM 判"该用工具 when 但没用"
        → 假 FAIL。动词+停用词的 match 一律滤掉;真工具(npu-smi / Bash 等)不受影响。
        """
        ext = InstructionExtractor()
        body = (
            "Use when facing 2+ independent tasks\n"   # use when → 停用词,不抽
            "run concurrently with others\n"           # run concurrently → 停用词,不抽
            "use of this tool is required\n"           # use of → 停用词,不抽
            "run npu-smi to check the card\n"          # npu-smi → 真工具,抽
            "use Bash to list files\n"                 # Bash → 真工具,抽
        )
        tool_reqs = [c for c in ext.extract(body) if c.source == InstructionSource.TOOL_REQUIREMENT]
        tools = sorted(c.params["tool_name"] for c in tool_reqs)
        assert tools == ["Bash", "npu-smi"]  # 只剩真工具;when/concurrently/of 被滤
        assert "when" not in tools
        assert "concurrently" not in tools

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

    def test_reference_usage_category_collapse(self):
        """路由型 skill 的类别级 references/<X>(无扩展名)是互斥备选 → 合并成一条;
        否则 N 类里 N-1 个不适用的算成假 FAIL(ascendc-tiling-design 曾 1/7 假红灯)。"""
        ext = InstructionExtractor()
        body = (
            "## References\n"
            "- reduction 算子见 references/reduction\n"
            "- broadcast 算子见 references/broadcast\n"
            "- elementwise 算子见 references/elewise\n"
        )
        refs = [c for c in ext.extract(body) if c.source == InstructionSource.REFERENCE_USAGE]
        # 3 个类别级 → 合并成 1 条(查任意 references/ 读),不是 3 条
        assert len(refs) == 1
        assert refs[0].check_method == CheckMethod.TOOL_CALL_CHECK
        assert refs[0].params.get("script_path") == "references/"

    def test_reference_usage_file_level_individual(self):
        """文件级 references/<X>.md 是独立必读,各自一条(读 1/3 不该算全过)。"""
        ext = InstructionExtractor()
        body = (
            "## References\n"
            "读 references/architecture.md 和 references/api.md 和 references/conventions.md\n"
        )
        refs = [c for c in ext.extract(body) if c.source == InstructionSource.REFERENCE_USAGE]
        assert len(refs) == 3  # 3 个文件级,各自一条(不合并)
        paths = sorted(r.params.get("script_path") for r in refs)
        assert paths == [
            "references/api.md",
            "references/architecture.md",
            "references/conventions.md",
        ]

    def test_reference_usage_category_mixed(self):
        """类别级 + 文件级混存:类别合并成 1 条,文件各自一条。"""
        ext = InstructionExtractor()
        body = (
            "## References\n"
            "- 各类算子见 references/reduction / references/broadcast\n"
            "- 通用规范必读 references/conventions.md\n"
        )
        refs = [c for c in ext.extract(body) if c.source == InstructionSource.REFERENCE_USAGE]
        # 1 条合并的类别(reduction+broadcast)+ 1 条文件(conventions.md)
        assert len(refs) == 2
        paths = sorted(r.params.get("script_path") for r in refs)
        assert paths == ["references/", "references/conventions.md"]

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

    def test_tool_requirement_cn_rejects_chinese_compound(self):
        """中文「运行时/运行中/运行失败」不是工具调用——「运行」是「运行时(runtime)」
        等词的一部分。旧 \\w+ 在 Unicode 下匹配汉字,把「运行时依赖基线」抽成
        tool_name='时依赖基线'(假阳性;ascendc-env-check 实测 6 条里 5 条垃圾)。
        设计本意(L83 注释)就是不抽中文工具名——要求工具名 ASCII 开头对齐该意图。"""
        ext = InstructionExtractor()
        body = "运行时依赖基线;运行失败;运行中的进程;运行时检测当前设备"
        tools = [c for c in ext.extract(body) if c.source == InstructionSource.TOOL_REQUIREMENT]
        names = [c.params.get("tool_name") for c in tools]
        assert "时依赖基线" not in names
        assert "失败" not in names
        assert "中的进程" not in names
        assert "时检测当前设备" not in names
        assert names == []  # 这段全是「运行X」复合词,没真工具调用

    def test_tool_requirement_cn_keeps_ascii_tool(self):
        """中文动词 + ASCII 工具名(含 - 和 .)照抽——真工具名 npu-smi/check_env.sh 都是 ASCII。"""
        ext = InstructionExtractor()
        body = "运行 npu-smi info 查设备;调用 check_env.sh;执行 get_npu_arch.py"
        tools = [c for c in ext.extract(body) if c.source == InstructionSource.TOOL_REQUIREMENT]
        names = [c.params.get("tool_name") for c in tools]
        assert any(n and n.startswith("npu") for n in names), names
        assert any(n and "check_env" in n for n in names), names
        assert any(n and "get_npu_arch" in n for n in names), names

    def test_constraint_cn_rejects_buke_compound(self):
        """「不可用/不可见」是普通词(unavailable/invisible),非禁止性约束,误命中会抽出
        「不可用时的回退方案见 X」这种路由提示(env-check 实测)。但「不可遗漏关键步骤」
        仍是真约束,得保留。"""
        ext = InstructionExtractor()
        body = (
            "不可用时的回退方案见 asys_commands.md。"
            "设备不可见时先排查驱动。"
            "不可遗漏任何检查项。"
        )
        constraints = [c for c in ext.extract(body) if c.source == InstructionSource.CONSTRAINT]
        texts = "".join(c.instruction_text for c in constraints)
        assert "不可用" not in texts
        assert "不可见" not in texts
        assert "不可遗漏" in texts  # 真约束保留

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

    def test_dual_rate_split(self, mocker, tmp_path: Path):
        """P0-1e: compliance_rate 拆 _programmatic(稳)+ _llm(漂,只看方向)。
        混合场景两率分别报;某类无指令则该率 None(不报假 0)。"""
        mocker.patch(
            "skill_eval.compliance.checker.call_claude",
            return_value='[{"index": 0, "followed": true, "evidence": "ok", "confidence": 0.9}]',
        )
        checker = ComplianceChecker()
        instructions = [
            InstructionCheck(  # 程序化,transcript 没提 Write → 不过
                source=InstructionSource.TOOL_REQUIREMENT,
                instruction_text="Use Write", section="S",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.TOOL_CALL_CHECK,
                params={"tool_name": "Write"},
            ),
            InstructionCheck(  # LLM_JUDGE,mock 判过
                source=InstructionSource.HEADING_STEP,
                instruction_text="Analyze the data", section="S",
                priority=InstructionPriority.REQUIRED,
                check_method=CheckMethod.LLM_JUDGE,
            ),
        ]
        report = checker.check(
            instructions=instructions,
            transcript="nothing about tools here",
            output_dir=tmp_path,
        )
        assert report.compliance_rate == 0.5  # 总:程序化 0 + LLM 1 = 1/2
        assert report.compliance_rate_programmatic == 0.0  # Write 不过 → 0/1
        assert report.compliance_rate_llm == 1.0  # mock 判过 → 1/1

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

    def test_reference_usage_category_grouped_check(self, tmp_path: Path):
        """合并后的类别指令(script_path='references/'):读任一 references/ 文件 → followed;
        啥都没读(捏造)→ not followed。修路由型 skill 假红灯:softmax 属 reduction,
        只读 reduction 是对的,合并后是 1/1 而非误导的 1/7。"""
        checker = ComplianceChecker()
        inst = InstructionCheck(
            source=InstructionSource.REFERENCE_USAGE,
            instruction_text="Consult reference material (7 alternative category references)",
            section="References",
            priority=InstructionPriority.RECOMMENDED,
            check_method=CheckMethod.TOOL_CALL_CHECK,
            params={"script_path": "references/"},
        )
        # Claude 路由到 reduction,读了 reduction 文件 → 过(就算只读 1 类也算对)
        trace_read = [{"name": "Read", "input_summary": '{"file_path": "/x/outputs/references/reduction/patterns.md"}'}]
        item = checker._check_instruction(
            inst, transcript="", output_dir=tmp_path, model=None, timeout=5, tool_trace=trace_read
        )
        assert item.followed is True
        assert item.source == "programmatic"
        # 啥参考都没读(只读了产出物)→ 不过(反捏造信号)
        trace_none = [{"name": "Read", "input_summary": '{"file_path": "/x/outputs/operator_spec.md"}'}]
        item2 = checker._check_instruction(
            inst, transcript="", output_dir=tmp_path, model=None, timeout=5, tool_trace=trace_none
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
        # 单次 call_claude 判整批"必须"候选(parser 修后该结构化判定确定,无需自洽)
        spy.assert_called_once()


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


class TestMatchToolCall:
    """Task 3: match_tool_call 公共谓词 —— tool_trace 命中判定(attr/dict 双兼容)。
    供 audit/auditor.py 跨模块复用 checker 的工具调用匹配逻辑。"""

    def test_hit_tool_name_attr_style(self):
        """tool_name 命中 tc.name(attr 风格 ToolCallRecord)→ True。"""
        trace = [ToolCallRecord(name="Read", input_summary="file.md")]
        assert match_tool_call("Read", "", trace) is True

    def test_hit_tool_name_dict_style(self):
        """tool_name 命中 tc.name(dict 风格)→ True。"""
        trace = [{"name": "Read", "input_summary": "file.md"}]
        assert match_tool_call("Read", "", trace) is True

    def test_hit_script_path_attr_style(self):
        """script_path 命中 tc.input_summary(attr 风格)→ True。"""
        trace = [ToolCallRecord(name="Read", input_summary="reading references/guide.md")]
        assert match_tool_call("", "references/guide.md", trace) is True

    def test_hit_script_path_dict_style(self):
        """script_path 命中 tc.input_summary(dict 风格)→ True。"""
        trace = [{"name": "Read", "input_summary": "reading references/guide.md"}]
        assert match_tool_call("", "references/guide.md", trace) is True

    def test_no_hit_returns_false(self):
        """tool_name / script_path 都不命中 → False。"""
        trace = [{"name": "Bash", "input_summary": "ls -la"}]
        assert match_tool_call("Read", "references/guide.md", trace) is False

    def test_empty_tool_trace(self):
        """空 tool_trace → False。"""
        assert match_tool_call("Read", "", []) is False

    def test_none_tool_trace(self):
        """None tool_trace → False。"""
        assert match_tool_call("Read", "", None) is False

    def test_case_insensitive(self):
        """大小写不敏感:READ 匹配 read,file.md 匹配 FILE.md。"""
        trace = [{"name": "read", "input_summary": "FILE.md"}]
        assert match_tool_call("READ", "file.md", trace) is True

    def test_namespace_attr_style(self):
        """简单 namespace(有 .name/.input_summary)也能匹配。"""
        trace = [SimpleNamespace(name="Write", input_summary="out.py")]
        assert match_tool_call("Write", "", trace) is True

    def test_partial_substring_match(self):
        """子串匹配:tool_name 是 tc.name 的子串(MCP 工具名长前缀场景)。"""
        trace = [{"name": "mcp__web_reader__webReader", "input_summary": "some url"}]
        assert match_tool_call("webReader", "", trace) is True

    def test_both_empty_returns_false(self):
        """tool_name 和 script_path 都空 → 不可能命中 → False。"""
        trace = [{"name": "Read", "input_summary": "file.md"}]
        assert match_tool_call("", "", trace) is False

    def test_first_match_wins_order(self):
        """多条 trace:首个命中的就返回 True(遍历顺序)。"""
        trace = [
            {"name": "Bash", "input_summary": "ls"},
            {"name": "Read", "input_summary": "wanted.md"},
        ]
        assert match_tool_call("Read", "", trace) is True


class TestExtractFirstBalancedPublic:
    """Task 3: extract_first_balanced 提为公共(供 audit 复用 JSON 抽取),
    _extract_first_balanced 保留为别名 —— checker 内部调用点零改动。"""

    def test_public_and_alias_are_same_object(self):
        """_extract_first_balanced 是 extract_first_balanced 的别名(同一函数对象)。"""
        assert _extract_first_balanced is extract_first_balanced

    def test_public_works_array(self):
        assert extract_first_balanced("[1, 2, 3]") == "[1, 2, 3]"

    def test_public_works_object(self):
        assert extract_first_balanced('{"a": 1}', "{", "}") == '{"a": 1}'

    def test_alias_still_works(self):
        """旧调用点走别名,行为不变。"""
        assert _extract_first_balanced("[1, 2, 3]") == "[1, 2, 3]"
