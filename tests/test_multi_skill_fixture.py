# tests/test_multi_skill_fixture.py
"""合成多 skill transcript fixture 的结构 + 切片纯度回归。

fixture(``fixtures/example-skills/multi-skill-demo/multi-skill.jsonl``)由确定性 builder 产出,
覆盖:三个 skill 交错调用 + log-error-analyzer 二次调用 + 子 agent 调 code-review-auditor
+ 子 agent 返回后主 agent 继续 log 工作。本测试固化其切片纯度,防止 builder / fixture 漂移。

这些是 *fixture 自身*的结构不变量(段数 / 无跨 skill 文件污染 / 子 agent 段作用域正确);
end-to-end audit(违规检测)由 Task 4 覆盖,不在此。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from sift.audit.slice import slice_transcript
from sift.evals.transcript import build_digest

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "example-skills" / "multi-skill-demo" / "multi-skill.jsonl"
BUILDER = FIXTURE.parent / "build_multi_skill.py"

LEA, CRA, RCZ = "log-error-analyzer", "code-review-auditor", "release-checklist-zh"

# 各 skill 的「领地」文件标记 —— 出现在别家 span 即为污染。
LEA_MARK = ("logs/", "error-report.json", "error-codes.md")
CRA_MARK = ("src/", "review-report.json", "code-patterns.md")
RCZ_MARK = ("CHANGELOG.md", "version.py", "dependency-policy.md", "pytest", "ruff", "git tag")


def _file_ops(span: list[dict]) -> list[tuple[str, str]]:
    """提取 span 内 tool_use 的文件操作:(tool_name, file_path 或 command)。"""
    ops: list[tuple[str, str]] = []
    for rec in span:
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name, inp = b.get("name"), b.get("input") or {}
                if name in ("Read", "Write", "Edit"):
                    ops.append((name, inp.get("file_path", "")))
                elif name == "Bash":
                    ops.append((name, inp.get("command", "")))
    return ops


def _ops_text(ops: list[tuple[str, str]]) -> str:
    return " ".join(v for _, v in ops)


@pytest.fixture(scope="module")
def spans_by_skill():
    return {s: slice_transcript(FIXTURE, s, "skill") for s in (LEA, CRA, RCZ)}


# ---- fixture 存在 + 与 builder 同步 ----------------------------------------


def test_fixture_committed_and_in_sync_with_builder():
    """committed jsonl 必须与确定性 builder 当前输出一致(防 builder 改了忘重生成)。"""
    assert FIXTURE.exists(), f"fixture 缺失: {FIXTURE}"
    spec = importlib.util.spec_from_file_location("build_multi_skill", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = "\n".join(json.dumps(r, ensure_ascii=False) for r in mod.build()) + "\n"
    committed = FIXTURE.read_text(encoding="utf-8")
    assert committed == fresh, "multi-skill.jsonl 与 builder 输出不一致:请重跑 builder"


# ---- 段数 ------------------------------------------------------------------


def test_log_error_analyzer_has_two_spans(spans_by_skill):
    assert len(spans_by_skill[LEA]) >= 2, "lea 被调两次,应至少 2 段"


def test_code_review_auditor_has_main_and_sub_spans(spans_by_skill):
    spans = spans_by_skill[CRA]
    assert len(spans) == 2, "cra: 主作用域 1 段 + 子 agent 1 段"
    # 恰好一段全主作用域、一段全子作用域(不依赖 spans 顺序)
    main_span = next((s for s in spans if {r.get("parent_tool_use_id") for r in s} == {None}), None)
    sub_span = next((s for s in spans if all(r.get("parent_tool_use_id") for r in s)), None)
    assert main_span is not None and sub_span is not None
    assert main_span is not sub_span


def test_release_checklist_has_one_span(spans_by_skill):
    assert len(spans_by_skill[RCZ]) == 1


# ---- 切片纯度(无跨 skill 文件污染)-----------------------------------------


def test_lea_spans_no_src_or_release_contamination(spans_by_skill):
    for span in spans_by_skill[LEA]:
        blob = _ops_text(_file_ops(span))
        assert "logs/" in blob, "lea span 应含 logs/ 文件"
        for bad in CRA_MARK + RCZ_MARK:
            assert bad not in blob, f"lea span 被污染:出现 {bad!r}"


def test_cra_spans_no_log_contamination(spans_by_skill):
    for span in spans_by_skill[CRA]:
        blob = _ops_text(_file_ops(span))
        assert "src/" in blob, "cra span 应含 src/ 文件"
        for bad in LEA_MARK + RCZ_MARK:
            assert bad not in blob, f"cra span 被污染:出现 {bad!r}"


def test_rcz_span_no_log_or_src_contamination(spans_by_skill):
    blob = _ops_text(_file_ops(spans_by_skill[RCZ][0]))
    for bad in LEA_MARK + CRA_MARK:
        assert bad not in blob, f"rcz span 被污染:出现 {bad!r}"


# ---- 子 agent 作用域正确 ----------------------------------------------------


def test_subagent_cra_span_scoped_to_agent_return(spans_by_skill):
    """子 agent 的 cra 段:全段 parent_tool_use_id 指向同一 Agent tool_use,
    且该 Agent tool_use 出现在主作用域 lea span2 里(子 agent 返回后主段继续)。"""
    sub_span = next(
        s for s in spans_by_skill[CRA] if all(r.get("parent_tool_use_id") for r in s)
    )
    parents = {r["parent_tool_use_id"] for r in sub_span}
    assert len(parents) == 1, "子 agent 段应全部归同一 Agent 调用"
    agent_tid = next(iter(parents))

    # 该 Agent tool_use 在主作用域(某 lea span)里能找到,且 id 匹配
    found = False
    for span in spans_by_skill[LEA]:
        for rec in span:
            for b in (rec.get("message") or {}).get("content") or []:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") == "Agent" and b.get("id") == agent_tid):
                    found = True
    assert found, f"子 agent tid {agent_tid} 未在主作用域 Agent tool_use 中找到"


def test_post_subagent_main_work_attributed_to_lea_not_cra(spans_by_skill):
    """子 agent 返回后,主 agent 继续的 logs/ 工作归 lea span2,不归 cra。"""
    # lea span2 应含 logs/security.log(Step 6 的活)
    lea_blob = _ops_text(_file_ops(spans_by_skill[LEA][-1]))
    assert "logs/security.log" in lea_blob, "子 agent 返回后的 logs 工作应归 lea 末段"
    # cra 的所有 span 都不含 logs/security.log
    for span in spans_by_skill[CRA]:
        assert "logs/security.log" not in _ops_text(_file_ops(span))


# ---- build_digest 不崩(切片能被 audit 重建) -------------------------------


def test_build_digest_on_each_skill_first_span(spans_by_skill, tmp_path):
    """每个 skill 首段写临时 jsonl 后 build_digest 不崩,且 skill_used 正确。"""
    for skill in (LEA, CRA, RCZ):
        span = spans_by_skill[skill][0]
        tp = tmp_path / f"{skill}.jsonl"
        tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in span), encoding="utf-8")
        digest = build_digest(tp)
        assert digest.skill_used == skill, f"{skill} 首段 skill_used 应为自身"
