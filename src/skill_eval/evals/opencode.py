"""opencode session(JSON)→ SessionDigest:把 opencode export 导出的单 session 解析成 digest。

**格式 = ``opencode export <sessionID>`` 的真实产出**(与 cc fixture 用真 cc jsonl 同理,
用真 opencode export 格式,非自造):顶层 ``{info, messages}``,messages 各带嵌套 ``parts``
(``message.info`` 含 role/agent),part 的 ``state`` 一体含 input+output。

与 cc 的 ``build_digest`` 平级(平行派,cc 代码零改动),重建逻辑镜像 cc
(read→input_files、write→produced_files、bash→commands),字段名按 opencode(filePath)。
parts 嵌在 message 里 → 不用像 cc 那样按 tool_use_id 两趟关联(更简单)。

复用 cc ``transcript`` 的共享重建 helper,cc 模块零改动。耦合到 cc 私有 helper 是已知
技术债(后续可把 helper 提为公共)。
"""
from __future__ import annotations

import json
from pathlib import Path

from skill_eval.evals.transcript import (
    SessionDigest, FileContent, Command,
    _strip_line_numbers, _detect_root, _rel, _BASH_WRITE_RE,
    _NAME_MAP, _norm_tool,
)
from skill_eval.models import ToolCallRecord


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
    已有序,此为保险)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    msgs = sorted(
        data.get("messages") or [],
        key=lambda m: ((m.get("info") or {}).get("time") or {}).get("created", 0),
    )
    return {"info": data.get("info") or {}, "messages": msgs}


def _file_path(inp: dict) -> str:
    """opencode 字段名容错:filePath / file_path / path 都试(版本差异)。"""
    return inp.get("filePath") or inp.get("file_path") or inp.get("path") or ""


def build_digest_from_opencode(path: Path, root: str | None = None) -> SessionDigest:
    """解析 opencode export 单 session → SessionDigest(重建逻辑镜像 cc build_digest)。

    假定 tool part 的 ``state.output`` / ``state.input.content`` 为字符串(export 文本输出
    均如此);若某版本存 dict/list,read/bash 分支会 TypeError —— 届时 guard,v1 不预判。

    注:opencode read 的 output 可能带 ``<path>...<content>...</content>`` XML 包裹(真 export
    如此),v1 原样保留(内容在、audit 可用);手写 fixture 用裸内容以与 cc 逐字节对照。
    """
    sess = load_opencode_session(path)
    info = sess["info"]

    user_turns: list[str] = []
    reads: dict[str, str] = {}            # path -> stripped initial content
    read_order: list[str] = []
    files: dict[str, str] = {}            # path -> 当前重建状态
    written: set[str] = set()
    commands: list[Command] = []
    all_paths: list[str] = []
    tool_trace: list[ToolCallRecord] = []
    transcript_text_parts: list[str] = []

    def remember(p: str) -> None:
        if p and p not in all_paths:
            all_paths.append(p)

    for m in sess["messages"]:
        mi = m.get("info") or {}
        role = mi.get("role")
        for p in m.get("parts") or []:
            ptype = p.get("type")
            if ptype == "text":
                t = p.get("text", "")
                if role == "user":
                    if t and not t.startswith("<"):
                        user_turns.append(t)
                elif role == "assistant" and t:
                    transcript_text_parts.append(t)
            elif ptype == "tool":
                tool = (p.get("tool") or "").lower()
                state = p.get("state") or {}
                inp = state.get("input") or {}
                tool_trace.append(ToolCallRecord(
                    name=tool,
                    input_summary=json.dumps(inp, ensure_ascii=False)[:200],
                ))
                if tool == "read":
                    fp = _file_path(inp)
                    remember(fp)
                    out = state.get("output") or ""
                    if fp and fp not in reads:
                        reads[fp] = _strip_line_numbers(out)
                        read_order.append(fp)
                    if fp and fp not in files:
                        files[fp] = reads[fp]
                elif tool == "write":
                    fp = _file_path(inp)
                    remember(fp)
                    if fp:
                        files[fp] = inp.get("content", "")
                        written.add(fp)
                elif tool == "bash":
                    cmd = inp.get("command", "")
                    commands.append(Command(cmd=cmd, stdout=(state.get("output") or "")[:2000]))
                    for mm in _BASH_WRITE_RE.finditer(cmd):
                        tgt = (mm.group(1) or "").strip("'\"")
                        if tgt:
                            remember(tgt)
                # edit/glob/grep/todowrite/webfetch/task:v1 只记 tool_trace,不重建

    if root is None:
        root = _detect_root(all_paths)
    produced = [FileContent(path=_rel(p, root), content=files.get(p, ""))
                for p in sorted(written)]
    inputs = [FileContent(path=_rel(p, root), content=reads[p])
              for p in read_order if p not in written]

    return SessionDigest(
        skill_used=info.get("agent"),
        user_turns=user_turns,
        input_files=inputs,
        produced_files=produced,
        commands=commands,
        tool_trace=tool_trace,
        transcript_text="\n".join(transcript_text_parts),
    )


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
            segments.append({
                "info": {**info_meta, "agent": target_agent},
                "messages": list(cur),
            })
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
        out.pop("filePath", None); out.pop("path", None)
        out.setdefault("file_path", fp)
    if name == "Edit":
        if out.get("oldString") is not None:
            out["old_string"] = out.pop("oldString")
        new = out.pop("newString", None)
        if new is None:
            new = out.pop("content", None)   # opencode 接受 content 作 newString 回退
        if new is not None:
            out["new_string"] = new
        if out.get("replaceAll") is not None:
            out["replace_all"] = out.pop("replaceAll")
    return out


def _strip_read_wrap(text: str) -> str:
    """opencode read output 可能带 <…><content>BODY</content></…> 包裹;有则取 BODY,
    无则原样(防御,对 bare fixture 与 cc 无害)。"""
    if "<content>" in text and "</content>" in text:
        i = text.find("<content>") + len("<content>")
        j = text.rfind("</content>")
        if 0 <= i <= j:
            return text[i:j]
    return text


def _records_from_opencode(path: Path) -> list[dict]:
    """opencode export JSON → cc-record 形 list(镜像 structured.load_structured_records,
    直接喂 _digest_from_records)。parts 嵌在 message → 单趟;input/output 在 state 一体。"""
    sess = load_opencode_session(path)
    out: list[dict] = []
    counter = 0
    for m in sess["messages"]:
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
                out_text = state.get("output") or ""
                if name == "Read":
                    out_text = _strip_read_wrap(out_text)
                blocks.append({"type": "tool_use", "id": tid, "name": name,
                               "input": _map_opencode_input(name, inp)})
                results.append((tid, out_text))
        out.append({"message": {"role": role, "content": blocks}, "parent_tool_use_id": None})
        if results:
            rblocks = [{"type": "tool_result", "tool_use_id": tid, "content": r} for tid, r in results]
            out.append({"message": {"role": "user", "content": rblocks}, "parent_tool_use_id": None})
    return out
