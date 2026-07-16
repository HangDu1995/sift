"""Tests for compliance engine and calibration checker."""

from __future__ import annotations

from types import SimpleNamespace

from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.audit.tool_match import extract_first_balanced, match_tool_call, _extract_first_balanced
from skill_eval.calibration.calibrator import CalibrationChecker
from skill_eval.models import (
    AuditReport,
    CheckMethod,
    ExpectationResult,
    GradingResult,
    GradingSummary,
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
        # 遵循性现在是 AuditReport;rate 从 summary 算 = pass/(pass+fail) = 1/4 = 0.25 → CONTRADICTS
        compliance = AuditReport(
            skill_name="test",
            transcripts=[],
            summary={"total": 4, "pass": 1, "fail": 3, "na": 0, "unclear": 0},
            findings=[],
        )
        report = cal.calibrate(grading, compliance)
        assert any(s.signal_type == "COMPLIANCE_CONTRADICTS_SCORING" for s in report.confidence_signals)

    def test_compliance_rate_none_skips_signal(self):
        """无可判定项(pass+fail=0,全 na/unclear)→ rate None → 不加 COMPLIANCE 信号,也不崩。"""
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="OK", source="programmatic"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        compliance = AuditReport(
            skill_name="test",
            transcripts=[],
            summary={"total": 5, "pass": 0, "fail": 0, "na": 5, "unclear": 0},  # 无可判定 → rate None
            findings=[],
        )
        report = cal.calibrate(grading, compliance)  # 不应 TypeError
        assert not any(s.signal_type == "COMPLIANCE_CONTRADICTS_SCORING" for s in report.confidence_signals)
        assert not any(s.signal_type == "COMPLIANCE_BACKS_SCORING" for s in report.confidence_signals)

    def test_compliance_rate_from_audit_summary(self):
        """calibrator 从 AuditReport.summary 算 rate(pass/(pass+fail),排 na+unclear)→ 信号阈值正确。"""
        cal = CalibrationChecker()
        grading = GradingResult(
            expectations=[
                ExpectationResult(text="e1", passed=True, evidence="OK", source="programmatic"),
            ],
            summary=GradingSummary(passed=1, failed=0, total=1, pass_rate=1.0),
        )
        # rate = 4/(4+1) = 0.8 → BACKS(na/unclear 排除出分母)
        backs = AuditReport(
            skill_name="t", transcripts=[],
            summary={"total": 6, "pass": 4, "fail": 1, "na": 1, "unclear": 0}, findings=[],
        )
        r1 = cal.calibrate(grading, backs)
        assert any(s.signal_type == "COMPLIANCE_BACKS_SCORING" for s in r1.confidence_signals)

        # rate = 1/(1+3) = 0.25 → CONTRADICTS
        contra = AuditReport(
            skill_name="t", transcripts=[],
            summary={"total": 4, "pass": 1, "fail": 3, "na": 0, "unclear": 0}, findings=[],
        )
        r2 = cal.calibrate(grading, contra)
        assert any(s.signal_type == "COMPLIANCE_CONTRADICTS_SCORING" for s in r2.confidence_signals)

        # rate = 3/(3+1) = 0.75 → 既不 BACKS(≥0.8)也不 CONTRADICTS(<0.5);na+unclear 不进分母
        mid = AuditReport(
            skill_name="t", transcripts=[],
            summary={"total": 6, "pass": 3, "fail": 1, "na": 1, "unclear": 1}, findings=[],
        )
        r3 = cal.calibrate(grading, mid)
        assert not any(s.signal_type == "COMPLIANCE_BACKS_SCORING" for s in r3.confidence_signals)
        assert not any(s.signal_type == "COMPLIANCE_CONTRADICTS_SCORING" for s in r3.confidence_signals)

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

    def test_tool_name_matches_command_in_input_summary(self):
        """tool_name 作为命令词在 tc.input_summary 命中 → True。

        Bash 子命令场景:SKILL.md「运行 pytest」抽出 tool_name=pytest,Claude 用 Bash
        跑 pytest(input_summary='{"command": "pytest"}')。旧逻辑只查 tc.name(bash)→
        漏判→假 FAIL;新分支按词边界查 input_summary 命中。"""
        trace = [{"name": "Bash", "input_summary": '{"command": "pytest"}'}]
        assert match_tool_call("pytest", "", trace) is True

    def test_tool_name_word_boundary_no_false_positive(self):
        """词边界防假 PASS:'git' 不命中 'cd widgets'(widgets 含 git 子串但非命令词)。

        裸子串会把 widgets / digital / concat.py 里的命令名词误判命中 → 漏报真违规。
        \\b 词边界:tool_name 须作为独立命令词出现(前后非字母数字)。"""
        trace = [{"name": "Bash", "input_summary": '{"command": "cd widgets && ls"}'}]
        assert match_tool_call("git", "", trace) is False


class TestExtractFirstBalancedPublic:
    """extract_first_balanced 是公共名(供 audit 复用 JSON 抽取),
    _extract_first_balanced 是其别名(旧调用点兼容)——两者现都从 audit.tool_match 来。"""

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
