"""结构化 records 输入:友好的 JSON → cc 记录形,复用现有 cc digest / slice 管线。

producer(insight 等)按 turn 吐::

    {"format": "sift-records",
     "records": [
        {"role": "user", "text": "..."},
        {"role": "assistant", "agent": "...", "parent_tool_use_id": null,
         "text": "...",
         "tool_calls": [{"name": "skill", "input": {"name": "x"}, "result": "..."},
                        {"name": "read",  "input": {"file_path": "..."}, "result": "..."}]}
     ]}

producer 不必拼 cc-jsonl 的 tool_use / tool_result 拆分、不必管 ``Skill`` 大小写 ——
本模块展开成 cc 记录(``{message:{role,content}, parent_tool_use_id[, agent]}``),
``build_digest`` / ``audit`` 零改动;``slice_transcript`` 按 ``--kind`` 选锚点切本单元段。

**``agent`` 字段透传到 cc-record**:producer(insight 等)按 turn 吐 ``agent``(= 该 turn 归属
的 agent 名),本 reader 把它挂到每条展开记录(主消息 + tool_result 回流记录都带)。切片器
``slice_transcript`` 的 ``kind="agent"`` 路据此切 agent 归属段——支持审"被 dispatch 的 agent"
(无 Skill 调用的纯 agent,如 developer)。**kind 分路、不并集**:同名既是 skill 又是 agent
(dual-nature,如 st-verifier)时,skill 面(``kind="skill"`` 按 Skill 调用切)与 agent 面
(``kind="agent"``)各审各的声明正文,互不串味。之前 v1 不消费(commit fdad6bf 标注的默契)是因
还没有 per-agent 切分的消费者;agent-audit 接通即消费。

识别:顶层 dict ``format == "sift-records"``(与 opencode 的 ``{info,messages}`` 区分)。
设计动机见 transcript-driven / 结构化输入;此处只做纯展开,无 LLM。
"""
from __future__ import annotations

import json
from pathlib import Path

from sift.errors import InputError
from sift.evals.transcript import _norm_tool, stamp_turn_indices

_FORMAT = "sift-records"


_OLD_FORMAT = "skill-eval-records"


def is_structured_records(path: Path) -> bool:
    """``.json`` 且顶层 ``format == "sift-records"`` → True(区分 opencode 形)。

    旧 id(``skill-eval-records``)不静默当 False:那会让旧 records 掉进 cc-jsonl
    解析、报「transcript 无可对账内容」误导诊断——显式报改名错误(cryptic → honest)。
    """
    if path.suffix != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("format") == _OLD_FORMAT:
        raise InputError(
            f"{path}: 旧格式 skill-eval-records 已改名 sift-records(工具 skill-eval → sift),"
            "请用新版 producer 重新导出后再喂 audit"
        )
    return data.get("format") == _FORMAT


def _rec(base: dict, agent) -> dict:
    """cc-record 带可选 ``agent``:有则挂上(切片器按 agent 归属切 agent 段),无则不挂(免每条都
    带个 null)。``agent`` = 该 turn 归属的 agent 名(insight 的 agentName),producer 吐、reader 透传。"""
    if agent:
        base["agent"] = agent
    return base


def load_structured_records(path: Path) -> list[dict]:
    """结构化 JSON → cc 记录形列表(可直接喂 ``_digest_from_records`` 或写 jsonl 给 slice)。

    每个 friendly record 展开为:一条主消息(text + tool_use 块);若有 result,
    再跟一条 user 消息装 tool_result(按 tool_use_id 回流,cc digest pass1 只从 user 收)。
    skill 调用归一:``name="skill"`` → ``"Skill"``、``input.name`` → ``input.skill``。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    counter = 0
    for rec in data.get("records") or []:
        role = rec.get("role") or "assistant"
        ptid = rec.get("parent_tool_use_id")
        agent = rec.get("agent")
        blocks: list[dict] = []
        results: list[tuple[str, object]] = []
        text = rec.get("text")
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in rec.get("tool_calls") or []:
            counter += 1
            tid = f"tu_{counter}"
            name = _norm_tool(tc.get("name") or "")
            inp = dict(tc.get("input") or {})
            if name == "Skill" and "skill" not in inp:
                # producer 用 input.name(insight 形)→ 归一到 input.skill(cc slicer 认)
                if inp.get("name"):
                    inp["skill"] = inp["name"]
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
            if tc.get("result") is not None:
                results.append((tid, tc.get("result")))
        out.append(_rec({"message": {"role": role, "content": blocks}, "parent_tool_use_id": ptid}, agent))
        if results:
            rblocks = [{"type": "tool_result", "tool_use_id": tid, "content": r} for tid, r in results]
            out.append(_rec({"message": {"role": "user", "content": rblocks}, "parent_tool_use_id": ptid}, agent))
    stamp_turn_indices(out)
    return out
