"""Run import contract: manifest membership, native call identity, and scope."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sift.audit.run import convert_run_member, find_target_calls, load_run_bundle
from sift.audit.slice import slice_transcript
from sift.errors import InputError
from sift.evals.structured import load_structured_records


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _native(role: str, content, timestamp: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": role, "message": {"role": role, "content": content}, "timestamp": timestamp}


def _bundle(tmp_path: Path, monkeypatch) -> Path:
    schema = {
        "$id": "urn:workflow-bundle:v1", "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object", "required": ["id", "data"],
        "properties": {"data": {"type": "object", "required": ["members"]}},
    }
    raw = json.dumps(schema).encode()
    (tmp_path / "workflow.schema.json").write_bytes(raw)
    monkeypatch.setattr("sift.audit.run.SCHEMA_SHA256", hashlib.sha256(raw).hexdigest())
    member = {"id": "m1", "kind": "session", "sessionId": "s1", "location": "sessions/s1.native"}
    manifest = {"schema": "workflow-bundle", "version": 1, "id": "run1", "kind": "workflow",
                "data": {"members": [{"id": "nested", "kind": "workflow", "data": {"members": [member]}}]}}
    (tmp_path / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_nested_member_two_skills_and_child_agent(tmp_path, monkeypatch):
    root = _bundle(tmp_path, monkeypatch)
    parent = root / "sessions/s1.native/s1.jsonl"
    child = root / "sessions/s1.native/subagents/c1.jsonl"
    _write_jsonl(parent, [
        _native("assistant", [
            {"type": "tool_use", "id": "a", "name": "skill", "input": {"name": "A"}},
            {"type": "tool_use", "id": "b", "name": "skill", "input": {"name": "B"}},
        ]),
        _native("user", [
            {"type": "tool_result", "tool_use_id": "a", "content": "A loaded"},
            {"type": "tool_result", "tool_use_id": "b", "content": "B loaded"},
        ]),
        _native("assistant", [{"type": "tool_use", "id": "d", "name": "task", "input": {"subagent_type": "reviewer"}}]),
        _native("user", [{"type": "tool_result", "tool_use_id": "d", "content": '<task id="c1" state="completed">ok</task>'}]),
    ])
    _write_jsonl(child, [_native("assistant", [{"type": "tool_use", "id": "r", "name": "read", "input": {"filePath": "x.txt"}}])])
    bundle = load_run_bundle(root)
    assert bundle.run_id == "run1" and len(bundle.members) == 1
    member = bundle.members[0]
    assert find_target_calls(member, "B", "skill") == ["b"]
    assert find_target_calls(member, "reviewer", "agent") == ["d"]

    data, warnings = convert_run_member(member)
    assert warnings == []
    assert data["records"][0]["tool_calls"][1]["result"] == "B loaded"
    child_record = next(record for record in data["records"] if record["native_session_id"] == "c1")
    assert child_record["agent"] == "reviewer"
    assert child_record["parent_tool_use_id"] == "d"
    assert child_record["tool_calls"][0]["input"]["file_path"] == "x.txt"

    v2 = root / "converted.json"
    v2.write_text(json.dumps(data), encoding="utf-8")
    jsonl = root / "expanded.jsonl"
    jsonl.write_text("\n".join(json.dumps(row) for row in load_structured_records(v2)), encoding="utf-8")
    spans = slice_transcript(jsonl, "B", "skill")
    assert len(spans) == 1
    assert spans[0][0]["_sift_slice_anchor_id"] == "b"
    assert len(slice_transcript(jsonl, "reviewer", "agent")) == 1


def test_schema_hash_and_path_boundary(tmp_path, monkeypatch):
    root = _bundle(tmp_path, monkeypatch)
    native = root / "sessions/s1.native/s1.jsonl"
    _write_jsonl(native, [_native("user", "hello")])
    (root / "workflow.schema.json").write_text("{}", encoding="utf-8")
    with pytest.raises(InputError, match="SHA-256"):
        load_run_bundle(root)

    _bundle(root, monkeypatch)
    manifest = json.loads((root / "workflow.json").read_text())
    manifest["data"]["members"][0]["data"]["members"][0]["location"] = "../outside.native"
    (root / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputError, match="越出"):
        load_run_bundle(root)


def test_run_rejects_unsupported_manifest_schema_name(tmp_path, monkeypatch):
    root = _bundle(tmp_path, monkeypatch)
    manifest = json.loads((root / "workflow.json").read_text())
    manifest["schema"] = "legacy.workflow-bundle"
    (root / "workflow.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputError, match="不支持的 run 契约版本"):
        load_run_bundle(root)


def test_child_association_uses_top_level_meta_tool_use_id(tmp_path, monkeypatch):
    root = _bundle(tmp_path, monkeypatch)
    parent = root / "sessions/s1.native/s1.jsonl"
    child = root / "sessions/s1.native/subagents/c1.jsonl"
    _write_jsonl(parent, [
        _native("assistant", [
            {"type": "tool_use", "id": "dispatch-1", "name": "task", "input": {"subagent_type": "reviewer"}},
        ]),
    ])
    _write_jsonl(child, [
        _native("assistant", [{"type": "tool_use", "id": "read-1", "name": "read", "input": {"filePath": "x.txt"}}]),
    ])
    child.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": "dispatch-1"}), encoding="utf-8")

    data, warnings = convert_run_member(load_run_bundle(root).members[0])
    assert warnings == []
    child_record = next(record for record in data["records"] if record["native_session_id"] == "c1")
    assert child_record["agent"] == "reviewer"
    assert child_record["parent_tool_use_id"] == "dispatch-1"


def test_two_same_skill_calls_in_one_message_are_distinct(tmp_path):
    path = tmp_path / "records.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "one", "name": "Skill", "input": {"skill": "B"}},
            {"type": "tool_use", "id": "two", "name": "Skill", "input": {"skill": "B"}},
        ]}},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "shared work"}]}},
    ])
    spans = slice_transcript(path, "B", "skill")
    assert [segment[0]["_sift_slice_anchor_id"] for segment in spans] == ["one", "two"]
    assert all(len(segment) == 2 for segment in spans)


def test_run_cli_rejects_mixed_input_and_root_before_reading_run(tmp_path):
    from sift.cli import app

    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: B\ndescription: test\n---\n# B\nRead before editing.\n", encoding="utf-8")
    runner = CliRunner()
    mixed = runner.invoke(app, ["audit", str(skill), "--run", str(tmp_path / "missing"),
                                "--transcript", str(tmp_path / "missing.jsonl")])
    assert mixed.exit_code == 2
    assert "互斥" in mixed.output
    root = runner.invoke(app, ["audit", str(skill), "--run", str(tmp_path / "missing"), "--kind", "root"])
    assert root.exit_code == 2
    assert "run 输入暂不支持 --kind root" in root.output


def test_run_cli_writes_source_aware_report_without_network(tmp_path, monkeypatch):
    from sift.cli import app
    from sift.models.audit_models import AuditReport

    root = tmp_path / "run"
    root.mkdir()
    _bundle(root, monkeypatch)
    _write_jsonl(root / "sessions/s1.native/s1.jsonl", [
        _native("assistant", [
            {"type": "tool_use", "id": "call-b-1", "name": "skill", "input": {"name": "B"}},
            {"type": "tool_use", "id": "call-b-2", "name": "skill", "input": {"name": "B"}},
        ]),
        _native("user", [
            {"type": "tool_result", "tool_use_id": "call-b-1", "content": "loaded"},
            {"type": "tool_result", "tool_use_id": "call-b-2", "content": "loaded again"},
        ]),
    ])
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: B\ndescription: test\n---\n# B\nRead before editing.\n", encoding="utf-8")

    class FakeAuditor:
        def audit(self, **kwargs):
            return AuditReport(skill_name="B", transcripts=[path.name for path in kwargs["transcripts"]],
                               summary={"total": 0, "pass": 0, "fail": 0, "na": 0}, findings=[])

    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", FakeAuditor)
    out = tmp_path / "out"
    result = CliRunner().invoke(app, ["audit", str(skill), "--run", str(root), "--kind", "skill",
                                     "--output", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads((out / "audit-report.json").read_text())
    assert report["run"]["runId"] == "run1"
    assert report["run"]["bundlePath"] == root.name
    assert str(tmp_path) not in (out / "audit-report.html").read_text()
    assert report["run"]["matchedSessionCount"] == 1
    assert report["run"]["targetCallCount"] == 2
    assert report["run"]["auditUnitCount"] == 2
    assert [unit["matchedToolUseIds"] for unit in report["run"]["units"]] == [["call-b-1"], ["call-b-2"]]
    assert report["transcript_sources"] == ["run", "run"]
    assert '"runId": "run1"' in (out / "audit-report.html").read_text()
