"""Transcript -> SessionDigest: 把 Claude Code session JSONL 解析成结构化 digest。
纯解析,无 LLM。供 transcript-driven eval 生成使用。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

_LINE_NUM_RE = re.compile(r"^\s*\d+\t", re.MULTILINE)


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

    # pass 2: walk in order —— 本任务只收 user_turns / skill_used
    user_turns: list[str] = []
    skill_used: str | None = None
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
        if role == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                    inp = b.get("input", {}) or {}
                    if not skill_used:
                        skill_used = inp.get("skill") or inp.get("name")

    return SessionDigest(skill_used=skill_used, user_turns=user_turns)
