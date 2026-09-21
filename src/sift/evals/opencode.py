"""opencode session(JSON)→ SessionDigest:把 opencode export 导出的单 session 解析成 digest。

**格式 = ``opencode export <sessionID>`` 的真实产出**(与 cc fixture 用真 cc jsonl 同理,
用真 opencode export 格式,非自造):顶层 ``{info, messages}``,messages 各带嵌套 ``parts``
(``message.info`` 含 role/agent),part 的 ``state`` 一体含 input+output。

**走 canonical 重建路径**(与 cc / structured 同一重建器,非平行派):``_records_from_opencode``
把 export 归一成 cc-record 形(工具名小写归大写、camelCase 字段映回 snake_case),再喂共享
重建器 ``_digest_from_records``(read 结果的 XML 包裹/``N: `` 行号清洗也由重建器统一做)。
Read/Write/Edit/MultiEdit 重建 + demoted/unreconstructed 推导自动获得,与 cc 同路径、产出等价(见
``test_cc_opencode_digest_symmetric``)。parts 嵌在 message → 单趟,不用像 cc 那样按
tool_use_id 两趟关联(更简单)。

``skill_used`` 取 ``info.agent``:opencode session 无 ``Skill`` tool_use(skill 在
``info.agent``),共享重建器只从 ``Skill`` tool_use 抽 → 抽不到 → ``build_digest_from_opencode`` 补回。
"""

from __future__ import annotations

import json
from pathlib import Path

from sift.evals.transcript import (
    SessionDigest,
    _digest_from_records,
    _norm_tool,
    stamp_turn_indices,
    summary_limit_for,
)
from sift.models import ToolCallRecord


def is_opencode_session(path: Path) -> bool:
    """opencode export 产物:.json 后缀 + 顶层有 info/messages 两键。"""
    if path.suffix != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and {"info", "messages"} <= set(data)


def transcript_source(path: Path) -> str:
    """transcript 来源标签(与 ``is_opencode_session`` 同判据):``'opencode'`` / ``'cc'``。

    供 audit 报告标注每条 transcript 来自哪侧(cc `.jsonl` / opencode export `.json`);
    未来新增格式在此扩展。与 rebuild 的分派同源,保证标注与实际解析路径一致。
    """
    return "opencode" if is_opencode_session(path) else "cc"


def load_opencode_session(path: Path) -> dict:
    """读 export JSON,返回 {info, messages}(messages 按 info.time.created 排序;export 本
    已有序,此为保险)。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    msgs = sorted(
        data.get("messages") or [],
        key=lambda m: ((m.get("info") or {}).get("time") or {}).get("created", 0),
    )
    return {"info": data.get("info") or {}, "messages": msgs}


def build_digest_from_opencode(path: Path, root: str | None = None, *, locate_turns: bool = False) -> SessionDigest:
    """解析 opencode export 单 session → SessionDigest(薄封装,与 cc build_digest 同形)。

    路径:``_records_from_opencode`` 把 opencode export 归一成 cc-record 形(字段映射)→
    共享重建器 ``_digest_from_records`` 走 cc 同款重建(read XML 包裹/行号清洗由重建器统一做)。
    Read/Write/Edit/MultiEdit 重建、demoted/unreconstructed 推导都自动获得(opencode 侧零重复实现)。

    ``skill_used`` 取 ``info.agent``:opencode session 无 ``Skill`` tool_use(它的 skill 在
    ``info.agent``),共享重建器只从 ``Skill`` tool_use 抽 → 抽不到 → 这里补回。
    ``locate_turns`` 透传重建器(控 ``[T{idx}] `` 前缀与 ``ToolCallRecord.turn``,默认关)。
    """
    sess = load_opencode_session(path)
    d = _digest_from_records(_records_from_opencode(path), root, locate_turns=locate_turns)
    agent = (sess.get("info") or {}).get("agent")
    if agent:
        d.skill_used = agent
    return d


def slice_opencode_by_agent(path: Path, target_agent: str) -> list[dict]:
    """按连续 message 的 info.agent == target_agent 切段。

    每段返回一个子 opencode export JSON(``{info, messages}``,parts 嵌在 message 里跟着走,
    无需单独过滤),info.agent 设为 target(rebuild 经 build_digest_from_opencode 取 skill_used)。
    target 未出现 → []。

    扁平切分(诚实局限):opencode 的 agent 字段无父子作用域,不同于 cc 的 parent_tool_use_id
    scope-aware 切分;子 agent(如 opencode task 工具)在扁平模型里就是另一段 agent run。
    """
    sess = load_opencode_session(path)
    info_meta = dict(sess["info"])
    msgs = sess["messages"]

    segments: list[dict] = []
    cur: list[dict] = []

    def _flush():
        if cur:
            segments.append(
                {
                    "info": {**info_meta, "agent": target_agent},
                    "messages": list(cur),
                }
            )
            cur.clear()

    for m in msgs:
        if (m.get("info") or {}).get("agent") == target_agent:
            cur.append(m)
        else:
            _flush()
    _flush()
    return segments


def _map_opencode_input(name: str, inp: dict) -> dict:
    """opencode camelCase → cc snake_case(文件操作工具)。其余原样。"""
    if name not in {"Read", "Write", "Edit"}:
        return dict(inp)
    out = dict(inp)
    fp = out.get("filePath") or out.get("file_path") or out.get("path")
    if fp is not None:
        out.pop("filePath", None)
        out.pop("path", None)
        out.setdefault("file_path", fp)
    if name == "Edit":
        if out.get("oldString") is not None:
            out["old_string"] = out.pop("oldString")
        new = out.pop("newString", None)
        if new is None:
            new = out.pop("content", None)  # opencode 接受 content 作 newString 回退
        if new is not None:
            out["new_string"] = new
        if out.get("replaceAll") is not None:
            out["replace_all"] = out.pop("replaceAll")
    return out


def _records_from_opencode(path: Path) -> list[dict]:
    """opencode export JSON → cc-record 形 list(镜像 structured.load_structured_records,
    直接喂 _digest_from_records)。parts 嵌在 message → 单趟;input/output 在 state 一体。

    已知限制(全局 seq 对齐):opencode 切片(``slice_opencode_by_agent``)返回 **message 子集**
    (子 session dict)→ CLI 写 ``.json`` slice 文件 → rebuild 经本函数重建 → 重建后按**段内位置**
    re-stamp ``_turn_index``(1,2,3…),非原全局位置。与 cc/structured 不同:那两者切片返回 **dict
    引用子集**,``_turn_index`` 骑在 dict 字段上过 JSON 往返 → rebuild 读回保住**原全局位置**。
    后果:opencode-sliced audit 的 turn_refs 为**段内 local**(v1 已知限制,见
    ``docs/superpowers/specs/2026-07-28-turn-refs-design.md`` 范围外节)。
    """
    sess = load_opencode_session(path)
    out: list[dict] = []
    counter = 0
    for m in sess["messages"]:
        counter = _append_opencode_message(m, out, counter)
    # 重建 dict 后 stamp:按 out 位置赋全局 _turn_index(与 cc/structured 同款 position 语义;
    # 在重建之后 stamp,新建的 dict 直接被标、无字段丢失风险——L157 重建不必显式拷字段)。
    stamp_turn_indices(out)
    return out


def build_subagent_tool_trace_opencode(transcript_path: Path) -> list[ToolCallRecord]:
    """kind=root 的 opencode 形 aux 旁路:**非主 agent** message 的全部工具痕迹。

    opencode 无父子作用域(扁平,per-message ``info.agent``),"子代理半边" = agent ≠
    ``session.info.agent`` 的全部 message——与 ``slice_opencode_by_agent`` 的 root 切法
    (主 agent 段)互补,合起来正好是整条 session。**无 agent 标注的 message 保守跳过**
    (归属不明,宁可不洗冤也不错洗)。工具名归一 / camelCase 字段映射复用
    ``_records_from_opencode`` 的同一套(``_norm_tool`` + ``_map_opencode_input``),
    与主 trace 同口径。

    与 cc 形的两个诚实差异(消费方按缺省处理,均有优雅回退):

    - ``turn`` 恒 ``None``:opencode 切片重建后按**段内位置** re-stamp(见
      ``_records_from_opencode`` 已知限制),aux 若标全量位置则与 root trace 的段内
      编号是两套体系,(T{n}) 会指错——宁缺毋错。
    - ``parent_tool_use_id`` 恒空:opencode 的派发(task 工具)与子代理 message 之间
      无 id 关联 → 派发引用 / pairs 行执行计数自动回退(关联不上的既有兜底路径)。
    """
    sess = load_opencode_session(transcript_path)
    main_agent = (sess.get("info") or {}).get("agent")
    if not main_agent:
        return []
    trace: list[ToolCallRecord] = []
    for m in sess.get("messages") or []:
        agent = (m.get("info") or {}).get("agent")
        if not agent or agent == main_agent:
            continue
        for p in m.get("parts") or []:
            if p.get("type") != "tool":
                continue
            state = p.get("state") or {}
            name = _norm_tool((p.get("tool") or "").lower() or "")
            inp = _map_opencode_input(name, state.get("input") or {})
            trace.append(
                ToolCallRecord(
                    name=name,
                    input_summary=json.dumps(inp, ensure_ascii=False)[: summary_limit_for(name)],
                    input=inp or None,
                    turn=None,
                    agent=str(agent or ""),
                )
            )
    return trace


def _append_opencode_message(m, out, counter):
    role = (m.get("info") or {}).get("role") or "assistant"
    blocks: list[dict] = []
    results: list[tuple[str, str]] = []
    for p in m.get("parts") or []:
        if p.get("type") == "text":
            t = p.get("text", "")
            if t:
                blocks.append({"type": "text", "text": t})
        elif p.get("type") == "tool":
            counter += 1
            tid = f"tu_{counter}"
            state = p.get("state") or {}
            inp = state.get("input") or {}
            name = _norm_tool((p.get("tool") or "").lower() or "")
            out_text = state.get("output") or ""  # 原样;read 的 XML 包裹/行号清洗归共享重建器
            blocks.append({"type": "tool_use", "id": tid, "name": name, "input": _map_opencode_input(name, inp)})
            results.append((tid, out_text))
    out.append({"message": {"role": role, "content": blocks}, "parent_tool_use_id": None})
    if not (results):
        return counter
    rblocks = [{"type": "tool_result", "tool_use_id": tid, "content": r} for tid, r in results]
    out.append({"message": {"role": "user", "content": rblocks}, "parent_tool_use_id": None})
    return counter
