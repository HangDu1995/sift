"""Transcript -> SessionDigest: 把 Claude Code session JSONL 解析成结构化 digest。
纯解析,无 LLM。供 transcript-driven eval 生成使用。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from skill_eval.models import ToolCallRecord

_LINE_NUM_RE = re.compile(r"^\s*\d+\t", re.MULTILINE)

_BASH_WRITE_RE = re.compile(
    r"""(?:>>?\s*|tee\s+(?:-a\s+)?)['"]?([^\s'";|&]+)"""
)


class FileContent(BaseModel):
    path: str
    content: str


class Command(BaseModel):
    cmd: str
    stdout: str


class SessionDigest(BaseModel):
    skill_used: str | None = None
    user_turns: list[str] = Field(default_factory=list)
    input_files: list[FileContent] = Field(default_factory=list)
    produced_files: list[FileContent] = Field(default_factory=list)
    unreconstructed: list[str] = Field(default_factory=list)
    commands: list[Command] = Field(default_factory=list)
    # 以下两字段为 audit 命令重建(工具指令对账 / 文本回放)预留,默认空,非侵入。
    tool_trace: list[ToolCallRecord] = Field(default_factory=list)
    transcript_text: str = ""

    @property
    def manifest(self) -> set[str]:
        return {f.path for f in self.input_files} | {f.path for f in self.produced_files}


def _strip_line_numbers(text: str) -> str:
    return _LINE_NUM_RE.sub("", text)


def _result_text(result_content) -> str:
    if isinstance(result_content, str):
        return result_content
    if isinstance(result_content, list):
        return "\n".join(
            b.get("text", "") for b in result_content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _detect_root(paths: list[str]) -> str:
    paths = [p for p in paths if p]
    if not paths:
        return ""
    if len(paths) == 1:
        return str(Path(paths[0]).parent) or "/"
    try:
        cp = os.path.commonpath(paths)
    except ValueError:
        return ""
    return os.path.dirname(cp) if os.path.isfile(cp) else cp


def _rel(path: str, root: str) -> str:
    if not path or not root:
        return path
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _records_from_cc(transcript_path: Path) -> list[dict]:
    """读 Claude Code session JSONL → record 列表（跳过空行 / 坏 JSON 行）。

    canonical 记录 = cc 记录形（``{message, parent_tool_use_id?}``）。抽出来供
    canonical 路径复用：cc importer 近乎恒等；opencode / insight importer 归一成
    同形 record，再喂 ``_digest_from_cc_records``。与 ``audit/slice.py`` 的 ``_records`` 同形。
    """
    records: list[dict] = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _digest_from_cc_records(records: list[dict], root: str | None = None) -> SessionDigest:
    """cc 记录列表（canonical 记录形）→ SessionDigest。纯内存、不读文件。

    从 ``build_digest`` 抽出，使「读文件」与「records→digest」分离：canonical
    importer（cc/opencode/insight）产 records，统一走本函数重建。
    """
    # pass 1: tool_use_id -> result text
    id2result: dict[str, str] = {}
    for rec in records:
        msg = rec.get("message") or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    id2result[b.get("tool_use_id", "")] = _result_text(b.get("content"))

    # pass 2: walk in order
    user_turns: list[str] = []
    skill_used: str | None = None
    reads: dict[str, str] = {}            # path -> stripped initial content
    read_order: list[str] = []
    files: dict[str, str] = {}            # path -> 当前重建状态
    written: set[str] = set()
    demoted: set[str] = set()
    bash_touched: set[str] = set()
    commands: list[Command] = []
    all_paths: list[str] = []
    tool_trace: list[ToolCallRecord] = []
    transcript_text_parts: list[str] = []

    def remember(p: str) -> None:
        if p and p not in all_paths:
            all_paths.append(p)

    for rec in records:
        msg = rec.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                t = content.strip()
            elif isinstance(content, list):
                t = "\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
            else:
                t = ""
            if t and not t.startswith("<system-reminder>") and "Caveat: The messages below" not in t:
                user_turns.append(t)
            continue
        if role != "assistant" or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                if b.get("text"):
                    transcript_text_parts.append(b.get("text", ""))
                continue
            if btype != "tool_use":
                continue
            name = b.get("name", "")
            inp = b.get("input", {}) or {}
            tid = b.get("id", "")
            # 无条件收集所有 tool_use 块(在按工具名分支之前):
            # 现有分支只覆盖 Skill/Read/Write/Edit/MultiEdit/Bash,
            # Glob/Grep/WebSearch 等会被忽略。tool_trace 必须收全部,
            # audit 的工具指令对账才能按任意工具名匹配。
            tool_trace.append(ToolCallRecord(
                name=name,
                input_summary=json.dumps(b.get("input") or {}, ensure_ascii=False)[:200],
            ))
            if name == "Skill":
                if not skill_used:
                    skill_used = inp.get("skill") or inp.get("name")
            elif name == "Read":
                fp = inp.get("file_path", "")
                remember(fp)
                if fp and fp not in reads:
                    reads[fp] = _strip_line_numbers(id2result.get(tid, ""))
                    read_order.append(fp)
                if fp and fp not in files:
                    files[fp] = reads[fp]
            elif name == "Write":
                fp = inp.get("file_path", "")
                remember(fp)
                if fp:
                    files[fp] = inp.get("content", "")
                    written.add(fp)
                    demoted.discard(fp)
            elif name == "Edit":
                fp = inp.get("file_path", "")
                remember(fp)
                old, new = inp.get("old_string", ""), inp.get("new_string", "")
                if fp and fp in files and old in files[fp]:
                    if inp.get("replace_all"):
                        files[fp] = files[fp].replace(old, new)
                    else:
                        files[fp] = files[fp].replace(old, new, 1)
                    written.add(fp)
                elif fp:
                    written.discard(fp)
                    demoted.add(fp)
            elif name == "MultiEdit":
                fp = inp.get("file_path", "")
                remember(fp)
                edits = inp.get("edits", []) or []
                cur, ok = files.get(fp), fp in files
                if ok:
                    for e in edits:
                        o, n = e.get("old_string", ""), e.get("new_string", "")
                        if o not in cur:
                            ok = False
                            break
                        cur = cur.replace(o, n, 1)
                if ok and fp:
                    files[fp] = cur
                    written.add(fp)
                elif fp:
                    written.discard(fp)
                    demoted.add(fp)
            elif name == "Bash":
                cmd = inp.get("command", "")
                commands.append(Command(cmd=cmd, stdout=id2result.get(tid, "")[:2000]))
                for m in _BASH_WRITE_RE.finditer(cmd):
                    tgt = (m.group(1) or "").strip("'\"")
                    if tgt:
                        bash_touched.add(tgt)

    if root is None:
        root = _detect_root(all_paths)

    produced = [FileContent(path=_rel(p, root), content=files.get(p, ""))
                for p in sorted(written) if p not in demoted]
    inputs = [FileContent(path=_rel(p, root), content=reads[p])
              for p in read_order if p not in written and p not in demoted]
    produced_paths = {f.path for f in produced}
    unrecon = sorted({_rel(p, root) for p in (demoted | bash_touched) if p})
    unrecon = [u for u in unrecon if u not in produced_paths]

    return SessionDigest(
        skill_used=skill_used, user_turns=user_turns,
        input_files=inputs, produced_files=produced,
        unreconstructed=unrecon, commands=commands,
        tool_trace=tool_trace, transcript_text="\n".join(transcript_text_parts),
    )


def build_digest(transcript_path: Path, root: str | None = None) -> SessionDigest:
    """Claude Code session JSONL → SessionDigest（读文件 → cc 记录 → digest）。

    薄封装：``_records_from_cc`` 读文件成 cc 记录（canonical 记录形），``_digest_from_cc_records``
    重建。拆分后各 importer（cc / opencode / insight）产 records 统一走 ``_digest_from_cc_records``。
    """
    return _digest_from_cc_records(_records_from_cc(transcript_path), root)
