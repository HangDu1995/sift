"""Prepare workflow run bundles for audit and attach run provenance to reports.

Validate the bundled schema and workflow manifest, enumerate session members,
find target Skill or agent calls, and convert selected .native sessions to
sift-records v2 inputs. The manifest defines membership. This module maps
the resulting audit units back to their run sessions and tool calls.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft7Validator

from sift.errors import InputError


SCHEMA_ID = "urn:workflow-bundle:v1"
SCHEMA_SHA256 = "96e1849c164abed1163a08fef866c8c204caa70a2812ed91c13ac38d8c9c8d07"
_TASK_ID = re.compile(r'<task\b[^>]*\bid="([^"]+)"')


@dataclass(frozen=True)
class RunMember:
    member_id: str
    session_id: str
    location: Path
    source: str


@dataclass(frozen=True)
class RunBundle:
    root: Path
    run_id: str
    members: tuple[RunMember, ...]
    warnings: tuple[str, ...]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: 无法读取 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: 顶层必须是 JSON 对象")
    return data


def _within(root: Path, raw: str) -> Path:
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise InputError(f"run 成员路径越出导出目录: {raw}")
    return candidate


def load_run_bundle(path: Path) -> RunBundle:
    root = path.resolve()
    if not root.is_dir():
        raise InputError(f"run 目录不存在: {path}")
    schema_path = root / "workflow.schema.json"
    try:
        raw_schema = schema_path.read_bytes()
    except OSError as exc:
        raise InputError(f"run 缺少 workflow.schema.json: {schema_path}") from exc
    # Trust the schema shipped with the run only when it matches the pinned
    # contract. It validates workflow.json membership, not session JSONL.
    digest = hashlib.sha256(raw_schema).hexdigest()
    if digest != SCHEMA_SHA256:
        raise InputError(f"{schema_path}: schema SHA-256 不匹配，期望 {SCHEMA_SHA256}，实际 {digest}")
    schema = _read_json(schema_path)
    if schema.get("$id") != SCHEMA_ID or schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
        raise InputError(f"{schema_path}: 不支持的 workflow schema 版本")
    manifest = _read_json(root / "workflow.json")
    if manifest.get("schema") != "workflow-bundle" or manifest.get("version") != 1:
        raise InputError(f"{root / 'workflow.json'}: 不支持的 run 契约版本")
    errors = sorted(Draft7Validator(schema).iter_errors(manifest), key=lambda e: tuple(str(p) for p in e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "<root>"
        raise InputError(f"{root / 'workflow.json'}: schema 校验失败 ({location}): {first.message}")

    members: list[RunMember] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def walk(node: dict) -> None:
        for member in node.get("data", {}).get("members", []):
            kind = member.get("kind")
            if kind == "workflow":
                walk(member)
                continue
            if kind != "session":
                continue
            sid, location = member.get("sessionId"), member.get("location")
            mid = str(member.get("id") or "")
            if not sid or not location:
                warnings.append(f"run 成员 {mid}: 缺少 sessionId 或 location，未导入")
                continue
            if sid in seen:
                raise InputError(f"run 清单重复引用 session {sid}")
            seen.add(sid)
            directory = _within(root, location)
            native = directory / f"{sid}.jsonl"
            if not native.resolve().is_relative_to(root):
                raise InputError(f"run 成员文件越出导出目录: {native}")
            if not directory.is_dir() or not native.is_file():
                warnings.append(f"run 成员 {mid}: 缺少 {native}，未导入")
                continue
            members.append(RunMember(mid, sid, directory, str(native.relative_to(root))))

    walk(manifest)
    return RunBundle(root, str(manifest["id"]), tuple(members), tuple(warnings))


def _native_lines(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(f"{path}:{line_no}: 无效 JSONL: {exc}") from exc
                if not isinstance(item, dict) or not isinstance(item.get("message"), dict):
                    raise InputError(f"{path}:{line_no}: 缺少 message 对象")
                yield item
    except OSError as exc:
        raise InputError(f"{path}: 无法读取 session: {exc}") from exc


def _blocks(item: dict) -> list[dict]:
    content = item["message"].get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _skill_name(block: dict) -> str | None:
    if block.get("type") != "tool_use" or str(block.get("name", "")).lower() != "skill":
        return None
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return None
    name = inp.get("skill") or inp.get("name")
    return str(name).rsplit(":", 1)[-1] if name else None


def _agent_name(block: dict) -> str | None:
    if block.get("type") != "tool_use" or str(block.get("name", "")).lower() not in {"task", "agent"}:
        return None
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return None
    name = inp.get("subagent_type") or inp.get("agent")
    return str(name) if name else None


def _session_files(member: RunMember) -> list[Path]:
    files = [member.location / f"{member.session_id}.jsonl"]
    child_dir = member.location / "subagents"
    if child_dir.is_dir():
        files.extend(sorted(path for path in child_dir.glob("*.jsonl") if path.resolve().is_relative_to(member.location)))
    return files


def find_target_calls(member: RunMember, target: str, kind: str | None) -> list[str]:
    """Scan every tool_use block, including two calls in one assistant message."""
    matches: list[str] = []
    for path in _session_files(member):
        for item in _native_lines(path):
            for block in _blocks(item):
                found = _agent_name(block) if kind == "agent" else _skill_name(block)
                if found == target:
                    matches.append(str(block.get("id") or ""))
    return matches


def _child_associations(files: list[Path]) -> dict[str, tuple[str, str, str]]:
    """Map child id to (dispatch id, role, parent native session id)."""
    dispatch: dict[str, tuple[str, str]] = {}
    children: dict[str, tuple[str, str, str]] = {}
    for path in files:
        for item in _native_lines(path):
            for block in _blocks(item):
                role = _agent_name(block)
                if role and block.get("id"):
                    dispatch[str(block["id"])] = (role, path.stem)
                if block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
                if tool_id not in dispatch or not isinstance(content, str):
                    continue
                match = _TASK_ID.search(content)
                if match:
                    role, parent_sid = dispatch[tool_id]
                    children[match.group(1)] = (tool_id, role, parent_sid)
    for path in files[1:]:
        sid = path.stem
        if sid in children:
            continue
        meta_path = path.with_suffix(".meta.json")
        if not meta_path.is_file():
            continue
        meta = _read_json(meta_path)
        tool_id = meta.get("toolUseId")
        if tool_id in dispatch:
            role, parent_sid = dispatch[str(tool_id)]
            children[sid] = (str(tool_id), role, parent_sid)
    return children


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    values = [str(block.get("text")) for block in content if isinstance(block, dict) and block.get("type") == "text" and block.get("text")]
    return "\n".join(values) or None


def _normalize_file_tool_input(name: str, inp: dict) -> dict:
    """Map the run's Read/Write/Edit arguments to Sift's expected names."""
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
            new = out.pop("content", None)
        if new is not None:
            out["new_string"] = new
        if out.get("replaceAll") is not None:
            out["replace_all"] = out.pop("replaceAll")
    return out


def convert_run_member(member: RunMember) -> tuple[dict, list[str]]:
    """Preserve native ids and per-session lineage in a v2 producer bundle."""
    files = _session_files(member)
    associations = _child_associations(files)
    records: list[dict] = []
    calls: dict[str, dict] = {}
    events: list[tuple[str, int, dict]] = []
    warnings: list[str] = []
    for order, path in enumerate(files):
        sid = path.stem
        if order and sid not in associations:
            warnings.append(f"{member.session_id}: 子会话 {sid} 无可靠派发关联，未导入")
            continue
        for item in _native_lines(path):
            events.append((str(item.get("timestamp") or ""), len(events), {"item": item, "sid": sid}))
    events.sort(key=lambda event: (event[0], event[1]))
    for _, _, event in events:
        item, sid = event["item"], event["sid"]
        message = item["message"]
        role = message.get("role") or item.get("type") or "assistant"
        parent_id, agent, parent_sid = associations.get(sid, (None, None, None))
        rec: dict[str, Any] = {
            "role": role,
            "session_id": member.session_id,
            "native_session_id": sid,
            "timestamp": item.get("timestamp"),
        }
        if parent_id:
            rec.update(parent_tool_use_id=parent_id, parent_native_session_id=parent_sid, agent=agent)
        text = _text(message.get("content"))
        if text:
            rec["text"] = text
        for block in _blocks(item):
            if block.get("type") == "tool_use":
                tool_id = str(block.get("id") or "")
                if not tool_id:
                    raise InputError(f"{member.source}: tool_use 缺少 id")
                if tool_id in calls:
                    raise InputError(f"{member.source}: 重复 tool_use.id {tool_id}")
                name = str(block.get("name") or "")
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    raise InputError(f"{member.source}: {tool_id} 的 input 不是对象")
                # The v2 reader normalizes names; file tools also need the
                # OpenCode camelCase-to-snake_case input mapping.
                normalized_name = name[:1].upper() + name[1:] if name.lower() in {"read", "edit", "write"} else name
                mapped = _normalize_file_tool_input(normalized_name, inp)
                tc = {"id": tool_id, "name": name, "input": mapped, "timestamp": item.get("timestamp")}
                rec.setdefault("tool_calls", []).append(tc)
                calls[tool_id] = tc
            elif block.get("type") == "tool_result":
                tool_id = str(block.get("tool_use_id") or "")
                if tool_id not in calls:
                    raise InputError(f"{member.source}: tool_result {tool_id} 找不到对应的 tool_use")
                calls[tool_id]["result"] = block.get("content")
                if "is_error" in block:
                    calls[tool_id]["is_error"] = block["is_error"]
        if rec.get("text") or rec.get("tool_calls"):
            records.append(rec)
    native_sessions = [{"id": member.session_id}]
    native_sessions.extend({"id": sid, "agent": agent, "parent_id": parent_sid}
                           for sid, (_, agent, parent_sid) in associations.items()
                           if any(p.stem == sid for p in files[1:]))
    bundle = {
        "format": "sift-records", "version": 2, "producer": "sift-run-import",
        "session": {"id": member.session_id, "native_sessions": native_sessions},
        "records": records, "captures": {}, "blobs": {},
    }
    return bundle, warnings


def prepare_run_transcripts(
    run_path: Path, name: str, kind: str | None, temporary_paths: list[Path]
) -> tuple[str, list[Path], list[dict], list[str]]:
    """Select matching sessions and write their sift-records audit inputs."""
    bundle = load_run_bundle(run_path)
    selected = []
    for member in bundle.members:
        matches = find_target_calls(member, name, kind)
        if kind is None:
            agent_matches = find_target_calls(member, name, "agent")
            matches = list(dict.fromkeys([*matches, *agent_matches]))
        if matches:
            selected.append((member, matches))
    if not selected:
        raise InputError(f"run {bundle.run_id}: 未找到 {name!r} 的 skill／agent 调用")

    transcripts: list[Path] = []
    sources: list[dict] = []
    warnings = list(bundle.warnings)
    for member, matches in selected:
        data, member_warnings = convert_run_member(member)
        warnings.extend(member_warnings)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            path = Path(stream.name)
        temporary_paths.append(path)
        transcripts.append(path)
        sources.append(
            {"memberId": member.member_id, "sessionId": member.session_id,
             "source": member.source, "matchedToolUseIds": matches}
        )
    return bundle.run_id, transcripts, sources, warnings


def build_run_report_metadata(
    run_id: str,
    bundle_path: Path,
    sources: list[dict],
    warnings: list[str],
    transcripts: list[Path],
    display_names: list[str] | None,
    kind: str | None,
    name: str,
) -> dict:
    """Associate each final audit input with its run session and tool call IDs."""
    units = []
    for idx, path in enumerate(transcripts):
        display = display_names[idx] if display_names is not None else path.name
        if kind:
            # Slice filenames retain the member session id and input ordinal.
            source = next((src for src in sources if display.startswith(f"{src['sessionId']}-{name}-")), None)
        else:
            source = next((src for src in sources if display == f"{src['sessionId']}.json"), None)
        if source is None:
            raise InputError(f"run 报告无法关联审计单元 {display} 的来源")
        ids = source["matchedToolUseIds"]
        if kind and path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as stream:
                if kind == "skill":
                    # The skill slicer records its exact invocation id.
                    first = json.loads(stream.readline())
                    anchor = first.get("_sift_slice_anchor_id")
                    if anchor:
                        ids = [anchor]
                else:
                    # A contiguous agent segment may contain several child
                    # sessions of the same role. Keep exactly their dispatches.
                    segment_ids = [json.loads(line).get("parent_tool_use_id") for line in stream if line.strip()]
                    ids = list(dict.fromkeys(tool_id for tool_id in segment_ids if tool_id))
        scope = "session" if not kind else "agent-segment" if kind == "agent" else "invocation"
        units.append({"auditUnitId": display, "memberId": source["memberId"],
                      "sessionId": source["sessionId"], "source": source["source"],
                      "scope": scope, "matchedToolUseIds": ids})
    return {"runId": run_id, "bundlePath": bundle_path.resolve().name, "matchedSessionCount": len(sources),
            "targetCallCount": sum(len(src["matchedToolUseIds"]) for src in sources),
            "auditUnitCount": len(units), "units": units, "warnings": warnings}
