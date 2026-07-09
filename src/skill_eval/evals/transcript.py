"""Transcript -> SessionDigest: 把 Claude Code session JSONL 解析成结构化 digest。
纯解析,无 LLM。供 transcript-driven eval 生成使用。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

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


def build_digest(transcript_path: Path, root: str | None = None) -> SessionDigest:
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
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name", "")
            inp = b.get("input", {}) or {}
            tid = b.get("id", "")
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
    )
