"""scope-aware 切片器:把多 skill transcript 按 Skill 工具调用切成单 skill 子 transcript 段。

归段:每条 record 按 parent_tool_use_id 归 agent 作用域(主/某子 agent);作用域内
last-invoked-owns(每个 Skill 调用起段,到同作用域下一个 Skill 调用或作用域结束)。
判定层(rebuild/judge/aggregate)不 import 本模块——本模块只产 record 段列表。
"""
from __future__ import annotations
import json
from pathlib import Path


def _records(path: Path) -> list[dict]:
    records: list[dict] = []
    for l in path.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if not l:
            continue
        try:
            records.append(json.loads(l))
        except json.JSONDecodeError:
            continue
    return records


def _content_blocks(rec: dict) -> list:
    """取 record.message.content 为 list;字符串或缺失→空列表。"""
    content = (rec.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _skill_name_of(rec: dict) -> str | None:
    """若 rec 含 Skill tool_use,返回 input.skill(剥命名空间前缀),否则 None。"""
    for b in _content_blocks(rec):
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
            full = (b.get("input") or {}).get("skill") or ""
            return full.rsplit(":", 1)[-1]  # "superpowers:writing-skills" → "writing-skills"
    return None


def _tool_result_ids(rec: dict) -> set[str]:
    """rec 内所有 tool_result 块的 tool_use_id 集合。"""
    return {
        b.get("tool_use_id", "")
        for b in _content_blocks(rec)
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
    }


def _find_agent_ids(records: list[dict]) -> set[str]:
    """收集所有 Agent tool_use 的 id(子 agent 作用域标记)。"""
    ids: set[str] = set()
    for rec in records:
        for b in _content_blocks(rec):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                tid = b.get("id", "")
                if tid:
                    ids.add(tid)
    return ids


def _scope_of(rec: dict) -> str | None:
    """record 的作用域:parent_tool_use_id 非空→该子 agent;否则 None(主)。"""
    ptid = rec.get("parent_tool_use_id")
    return ptid if ptid else None


def slice_transcript_by_skill(transcript_path: Path, target_skill: str) -> list[list[dict]]:
    """scope-aware 切 target_skill 的段。target 从未被调用→返回 []。

    两趟扫描:
      1. 找出所有 Agent tool_use id(子 agent 作用域标记)。
      2. 按 parent_tool_use_id 归作用域;作用域内 last-invoked-owns。
         子 agent 作用域在 Agent tool_result 处结束;主作用域到 transcript 末。
    """
    records = _records(transcript_path)
    if not records:
        return []

    agent_ids = _find_agent_ids(records)

    # 作用域内状态:active = 当前生效 skill 名;cur = 当前段的 record 下标列表。
    scope_state: dict[str | None, dict] = {}
    span_index_lists: list[list[int]] = []  # 每段 = record 下标列表

    def _flush(state: dict) -> None:
        if state.get("active") == target_skill and state.get("cur"):
            span_index_lists.append(state["cur"])

    for i, rec in enumerate(records):
        scope = _scope_of(rec)
        state = scope_state.setdefault(scope, {"active": None, "cur": []})

        name = _skill_name_of(rec)
        if name is not None:
            # 同作用域新 Skill 调用:关掉当前 target 段(如有),再切 active
            if state["active"] == target_skill and state["cur"]:
                _flush(state)
            state["active"] = name
            state["cur"] = [i] if name == target_skill else []
            continue

        # 非 Skill record:若当前作用域 active 是 target,归入段
        if state["active"] == target_skill:
            state["cur"].append(i)

        # depth-1 closure only:主作用域里的 Agent tool_result 结束对应子 agent 作用域。
        # 嵌套子 agent(深度≥2)的 Agent tool_result 出现在父作用域(scope≠None),不在此
        # 触发;它们在 transcript 末统一 flush——无害,因为已返回 agent 的 scope 下不再
        # 出现 record(后续 record 归父作用域)。
        if scope is None:
            for tid in _tool_result_ids(rec):
                if tid in agent_ids:
                    sub = scope_state.get(tid)
                    if sub:
                        _flush(sub)
                        sub["active"] = None
                        sub["cur"] = []

    # transcript 末:刷掉所有仍活跃的段
    for state in scope_state.values():
        _flush(state)

    spans: list[list[dict]] = []
    for indices in span_index_lists:
        spans.append([records[k] for k in indices])

    return spans
