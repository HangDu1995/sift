"""opencode session(JSON)→ SessionDigest:把 opencode 单 session JSON 解析成 digest。

平行派(非归一):与 cc 的 ``build_digest`` 平级,opencode 的 message/part 结构、tool 一体
的 state、per-message agent 字段都走原生解析,不捏成 cc record 形状。重建逻辑镜像 cc
(read→input_files、write→produced_files、bash→commands),字段名按 opencode(filePath)。

复用 cc ``transcript`` 的共享重建 helper(_strip_line_numbers/_detect_root/_rel/
_BASH_WRITE_RE + FileContent/Command/SessionDigest),cc 模块零改动。耦合到 cc 私有 helper
是已知技术债(后续可把 helper 提为公共)。
"""
from __future__ import annotations

import json
from pathlib import Path

from skill_eval.evals.transcript import (
    SessionDigest, FileContent, Command,
    _strip_line_numbers, _detect_root, _rel, _BASH_WRITE_RE,
)
from skill_eval.models import ToolCallRecord


def is_opencode_session(path: Path) -> bool:
    """opencode session JSON:.json 后缀 + 顶层有 session/messages/parts 三键。"""
    if path.suffix != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict) and {"session", "messages", "parts"} <= set(data)


def load_opencode_session(path: Path) -> dict:
    """读 JSON,按 time_created 排序 messages/parts,返回 {session, messages, parts}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    msgs = sorted(data.get("messages") or [], key=lambda m: m.get("time_created", 0))
    parts = sorted(data.get("parts") or [], key=lambda p: p.get("time_created", 0))
    return {"session": data.get("session") or {}, "messages": msgs, "parts": parts}


def _file_path(inp: dict) -> str:
    """opencode 字段名容错:filePath / file_path / path 都试(版本差异)。"""
    return inp.get("filePath") or inp.get("file_path") or inp.get("path") or ""


def build_digest_from_opencode(path: Path, root: str | None = None) -> SessionDigest:
    """解析 opencode 单 session JSON → SessionDigest(重建逻辑镜像 cc build_digest)。

    假定 tool part 的 ``state.output`` / ``state.input.content`` 为字符串(合成 fixture
    与真实 opencode 文本输出均如此);若某 opencode 版本把 output 存成 dict/list,read/bash
    分支的 ``_strip_line_numbers(out)`` / 切片会 TypeError —— 届时在此 guard,v1 不预判。
    """
    sess = load_opencode_session(path)
    parts_by_msg: dict[str, list[dict]] = {}
    for p in sess["parts"]:
        parts_by_msg.setdefault(p.get("message_id"), []).append(p)

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
        mobj = m.get("data") or {}
        role = mobj.get("role")
        for p in parts_by_msg.get(m.get("id"), []):
            pdata = p.get("data") or {}
            ptype = pdata.get("type")
            if ptype == "text":
                t = pdata.get("text", "")
                if role == "user":
                    if t and not t.startswith("<"):
                        user_turns.append(t)
                elif role == "assistant" and t:
                    transcript_text_parts.append(t)
            elif ptype == "tool":
                tool = (pdata.get("tool") or "").lower()
                state = pdata.get("state") or {}
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
        skill_used=sess["session"].get("agent"),
        user_turns=user_turns,
        input_files=inputs,
        produced_files=produced,
        commands=commands,
        tool_trace=tool_trace,
        transcript_text="\n".join(transcript_text_parts),
    )
