# tests/test_transcript.py
import json
from pathlib import Path
from skill_eval.evals.transcript import build_digest, SessionDigest, _strip_line_numbers, FileContent, Command

def _write_jsonl(tmp_path: Path, records: list) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p

def _user(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}

def test_strip_line_numbers():
    raw = "1\talpha\n2\tbeta\n"
    assert _strip_line_numbers(raw) == "alpha\nbeta\n"

def test_user_turns_extracted_and_noise_skipped(tmp_path):
    recs = [
        {"type": "summary", "content": "meta"},                       # 无 message,跳过
        _user("正经用户请求"),                                         # 收
        _user("<system-reminder>cache</system-reminder>"),            # 跳过
        {"message": {"role": "user", "content": [                     # tool_result,跳过
            {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        _user("第二个请求"),
    ]
    d = build_digest(_write_jsonl(tmp_path, recs))
    assert isinstance(d, SessionDigest)
    assert d.user_turns == ["正经用户请求", "第二个请求"]

def _asst_blocks(blocks: list) -> dict:
    return {"message": {"role": "assistant", "content": blocks}}

def test_skill_used_extracted_from_skill_tool_use(tmp_path):
    recs = [
        _asst_blocks([{"type": "tool_use", "id": "s1", "name": "Skill",
                       "input": {"skill": "my-skill"}}]),
    ]
    d = build_digest(_write_jsonl(tmp_path, recs))
    assert d.skill_used == "my-skill"

def _asst(tool_uses: list) -> dict:
    return {"message": {"role": "assistant", "content": tool_uses}}

def _tool_use(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def _tool_result(tid: str, text: str) -> dict:
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}

def test_read_becomes_input_file_with_line_numbers_stripped(tmp_path):
    recs = [
        _asst([_tool_use("r1", "Read", {"file_path": "/proj/calc.py"})]),
        _tool_result("r1", "1\tx = 1\n2\ty = 2\n"),
    ]
    d = build_digest(_write_jsonl(tmp_path, recs), root="/proj")
    assert d.input_files == [FileContent(path="calc.py", content="x = 1\ny = 2\n")]
    assert d.produced_files == []

def test_bash_recorded_as_command(tmp_path):
    recs = [
        _asst([_tool_use("b1", "Bash", {"command": "python cli.py '1+2*3'", "description": ""})]),
        _tool_result("b1", "7\n"),
    ]
    d = build_digest(_write_jsonl(tmp_path, recs), root="/proj")
    assert d.commands == [Command(cmd="python cli.py '1+2*3'", stdout="7\n")]

def test_relativization_via_common_prefix_when_root_none(tmp_path):
    recs = [
        _asst([_tool_use("r1", "Read", {"file_path": "/proj/pkg/calc.py"})]),
        _tool_result("r1", "1\tcore\n"),
    ]
    d = build_digest(_write_jsonl(tmp_path, recs))   # root=None
    assert d.input_files[0].path == "calc.py"   # 单文件→parent 作 root
