"""越界视图(一期:领域工具对账,信息性)测试。

分类三分:builtin(lower 口径,cc 大写/opencode 小写双形统一)/ domain(命中声明工具
要求)/ undeclared-domain(未命中,视图主体)。纯聚合零 LLM;不产 finding、不进 verdict。
声明侧抽取三源:TOOL_REQUIREMENT.params.tool_name / mcp__server__tool 全名 / 反引号代码字。
"""

import json

from sift.audit.boundary import BUILTIN_TOOLS, aggregate_boundary, declared_tool_names
from sift.models.audit_models import BoundarySummary, ToolUsageEntry
from sift.models.batch_models import InstructionCheck
from sift.models.enums import CheckMethod, InstructionPriority, InstructionSource
from sift.models.execution_models import ToolCallRecord


def _inst(text: str, source=InstructionSource.CONSTRAINT, tool_name: str | None = None,
          headline: str = "", excerpt: str = "") -> InstructionCheck:
    params = {"tool_name": tool_name} if tool_name else {}
    return InstructionCheck(
        source=source, instruction_text=text, section="Rules",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.TOOL_CALL_CHECK if tool_name else CheckMethod.LLM_JUDGE,
        params=params, headline=headline, source_excerpt=excerpt,
    )


def _trace(*names: str) -> list[ToolCallRecord]:
    return [ToolCallRecord(name=n, input={}) for n in names]


# ---------------------------------------------------------------------------
# 内置表 + 分类
# ---------------------------------------------------------------------------

def test_builtin_table_members_all_lowercase():
    """表内成员全小写(判定口径 name.lower() in BUILTIN_TOOLS 的前提)。"""
    assert all(t == t.lower() for t in BUILTIN_TOOLS)
    # 关键成员在表里:原语 / 子代理 / skill 进入信号
    assert {"read", "bash", "grep", "glob", "task", "skill"} <= BUILTIN_TOOLS


def test_builtin_tools_skipped_across_case_forms():
    """cc 大写开头 / opencode 小写透传双形都命中 builtin → 不进视图。"""
    s = aggregate_boundary([("t.jsonl", _trace("Read", "read", "GREP", "Bash", "Skill", "Task"))], [])
    assert s.total_tools == 0 and s.tools == []


def test_mcp_tool_without_declaration_is_undeclared():
    s = aggregate_boundary([("t.jsonl", _trace("mcp__db__query"))], [])
    assert s.total_tools == 1 and s.undeclared == 1 and s.declared == 0
    e = s.tools[0]
    assert e.name == "mcp__db__query" and e.classification == "undeclared-domain"
    assert e.declared_by == ""


# ---------------------------------------------------------------------------
# 声明侧抽取(三源)
# ---------------------------------------------------------------------------

def test_declared_tool_names_from_tool_requirement_params():
    """TOOL_REQUIREMENT.params.tool_name 是最可靠来源(extractor 已抽好)。"""
    insts = [_inst("必须使用 codeql 扫描", source=InstructionSource.TOOL_REQUIREMENT,
                   tool_name="codeql", headline="用 codeql 扫描")]
    declared = declared_tool_names(insts)
    assert "codeql" in declared
    assert declared["codeql"] == "用 codeql 扫描"  # 显示文本 headline 优先


def test_declared_tool_names_mcp_full_name_from_any_instruction_text():
    """约束/禁令文本里的 mcp__server__tool 全名也算声明(划边界的 MCP 工具)。"""
    insts = [_inst("只许用 mcp__db__query 查数据,禁止直连生产库",
                   excerpt="- 只许用 `mcp__db__query` 查数据")]
    declared = declared_tool_names(insts)
    assert "mcp__db__query" in declared


def test_declared_tool_names_backtick_token():
    insts = [_inst("调用 `custom-lint` 做静态检查")]
    assert "custom-lint" in declared_tool_names(insts)


def test_declared_tool_names_case_insensitive_match():
    """声明写 CodeQL、trace 里 codeql → 命中(双形大小写由 lower 统一)。"""
    insts = [_inst("run CodeQL scan", source=InstructionSource.TOOL_REQUIREMENT,
                   tool_name="CodeQL")]
    s = aggregate_boundary([("t.jsonl", _trace("codeql"))], insts)
    assert s.declared == 1 and s.tools[0].classification == "domain"


def test_declared_tool_names_empty_when_no_tool_refs():
    assert declared_tool_names([_inst("输出必须是 JSON 格式")]) == {}


# ---------------------------------------------------------------------------
# 对账 + 聚合
# ---------------------------------------------------------------------------

def test_declared_hit_carries_declared_by_and_is_domain():
    insts = [_inst("只许用 mcp__db__query 查数据")]
    s = aggregate_boundary([("t.jsonl", _trace("mcp__db__query", "mcp__web__fetch"))], insts)
    assert s.total_tools == 2 and s.declared == 1 and s.undeclared == 1
    hit = next(e for e in s.tools if e.classification == "domain")
    assert hit.name == "mcp__db__query" and "mcp__db__query" in hit.declared_by


def test_sessions_and_calls_across_transcripts():
    """sessions = 出现于几个 transcript(去重);calls = 总调用次数;transcripts 列文件名。"""
    s = aggregate_boundary([
        ("a.jsonl", _trace("mcp__db__query", "mcp__db__query")),
        ("b.jsonl", _trace("mcp__db__query")),
    ], [])
    e = s.tools[0]
    assert e.sessions == 2 and e.calls == 3 and e.transcripts == ["a.jsonl", "b.jsonl"]


def test_sorting_undeclared_first_then_domain():
    """undeclared-domain 先(sessions 降序),再 domain(calls 降序)。"""
    s = aggregate_boundary([
        ("a.jsonl", _trace("mcp__x__one")),                 # undeclared, sessions=1
        ("a.jsonl", _trace("mcp__x__two")),                 # undeclared, sessions=1
        ("b.jsonl", _trace("mcp__x__two")),                 # undeclared, sessions=2 → 排最前
        ("a.jsonl", _trace("mcp__decl__p", "mcp__decl__p", "mcp__decl__p")),  # domain calls=3
        ("a.jsonl", _trace("mcp__decl__q")),                # domain calls=1
    ], [_inst("用 mcp__decl__p 与 mcp__decl__q")])
    order = [(e.name, e.classification) for e in s.tools]
    assert order == [
        ("mcp__x__two", "undeclared-domain"),   # sessions=2
        ("mcp__x__one", "undeclared-domain"),   # sessions=1,name序
        ("mcp__decl__p", "domain"),             # calls=3
        ("mcp__decl__q", "domain"),             # calls=1
    ]


def test_empty_input_all_zero():
    s = aggregate_boundary([], [])
    assert s == BoundarySummary()


def test_model_defaults_and_roundtrip():
    """ToolUsageEntry/BoundarySummary 默认值 + JSON 往返(旧 report 兼容的前提)。"""
    assert BoundarySummary().total_tools == 0 and BoundarySummary().tools == []
    s = BoundarySummary(total_tools=1, undeclared=1, tools=[ToolUsageEntry(
        name="mcp__x__y", classification="undeclared-domain")])
    restored = BoundarySummary.model_validate_json(s.model_dump_json())
    assert restored.tools[0].name == "mcp__x__y"
    assert restored.tools[0].sessions == 0 and restored.tools[0].transcripts == []


# ---------------------------------------------------------------------------
# audit() 接线(集成)
# ---------------------------------------------------------------------------

def test_audit_report_boundary_field_end_to_end(monkeypatch, tmp_path):
    """audit() 产出 boundary:fixture transcript 只用 Read(builtin)→ 视图空、不误报;
    声明正文里的工具要求真实参与对账(declarations 来自 refine 后 instructions)。"""
    from pathlib import Path
    from sift.audit.auditor import TranscriptAuditor

    monkeypatch.setattr(
        "sift.audit.auditor.call_claude",
        lambda *a, **k: '[{"index":0,"violated":false,"evidence":"未违规"}]',
    )
    cc = Path(__file__).parent.parent / "fixtures" / "example-skills" / \
        "log-error-analyzer-zh" / "transcripts" / "synthetic-violation.jsonl"
    report = TranscriptAuditor().audit("lea", "# x\n\nNEVER fabricate data.\n", [cc])
    # 该 transcript 全部工具调用是 Read(builtin)→ 不进视图
    assert report.boundary.total_tools == 0
    assert report.boundary.undeclared == 0


def test_audit_report_boundary_no_declared_tools(monkeypatch, tmp_path):
    """非 builtin 工具出现且声明侧无任何工具名 → 全 undeclared(declared=0,头部标注口径)。"""
    from sift.audit.auditor import TranscriptAuditor

    monkeypatch.setattr(
        "sift.audit.auditor.call_claude",
        lambda *a, **k: '[{"index":0,"violated":false,"evidence":"未违规"}]',
    )
    # 构造只含一次 mcp 工具调用的 transcript
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "查点东西"}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "mcp__db__query", "id": "t1", "input": {"sql": "select 1"}}]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}),
        json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]
    tp = tmp_path / "session.jsonl"
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = TranscriptAuditor().audit("s", "# x\n\nNEVER fabricate data.\n", [tp])
    assert report.boundary.total_tools == 1
    assert report.boundary.undeclared == 1 and report.boundary.declared == 0
    e = report.boundary.tools[0]
    assert e.name == "mcp__db__query" and e.sessions == 1 and e.calls == 1
