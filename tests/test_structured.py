# Copyright (c) 2025-2026 Huawei Technologies Co., Ltd.
# 结构化 records 输入(reader)的单测:友好 JSON → cc 记录形 → 现有 digest/slice。
import json

from skill_eval.evals.structured import is_structured_records, load_structured_records
from skill_eval.evals.transcript import _digest_from_cc_records


def _write(tmp_path, obj, name="s.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


# insight 直译形态:per-turn {role, text, agent?, tool_calls:[{name, input, result}]}。
# skill 用小写 + input.name(opencode/insight 形),read/write 同理小写 —— reader 应归一到 cc 大写。
FRIENDLY = {
    "format": "skill-eval-records",
    "records": [
        {"role": "user", "text": "分析 x.py 的崩溃"},
        {
            "role": "assistant",
            "agent": "kernel-developer",
            "parent_tool_use_id": None,
            "text": "调用 skill 排查",
            "tool_calls": [
                {"name": "skill", "input": {"name": "ascendc-crash-debug"},
                 "result": '<skill_content name="ascendc-crash-debug">BODY</skill_content>'},
                {"name": "read", "input": {"file_path": "src/x.py"}, "result": "1\timport os\n2\tcrash()"},
            ],
        },
    ],
}


def _uses(recs):
    return [b for r in recs for b in (r.get("message", {}).get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def test_is_structured_records_detection(tmp_path):
    assert is_structured_records(_write(tmp_path, FRIENDLY))
    assert not is_structured_records(_write(tmp_path, {"info": {}, "messages": []}, "oc.json"))  # opencode 形
    assert not is_structured_records(tmp_path / "nope.jsonl")                                   # 非 .json


def test_skill_tool_call_normalized_to_Skill(tmp_path):
    recs = load_structured_records(_write(tmp_path, FRIENDLY))
    names = {u["name"] for u in _uses(recs)}
    assert "Skill" in names          # skill → Skill
    assert "Read" in names           # read → Read(大写,cc digest 分支认)
    skill_use = next(u for u in _uses(recs) if u["name"] == "Skill")
    assert skill_use["input"]["skill"] == "ascendc-crash-debug"   # input.name → input.skill


def test_tool_results_emitted_as_user_record_with_matching_ids(tmp_path):
    recs = load_structured_records(_write(tmp_path, FRIENDLY))
    use_ids = [u["id"] for u in _uses(recs)]
    res_ids = [b["tool_use_id"] for r in recs
               for b in (r.get("message", {}).get("content") or [])
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert set(res_ids) == set(use_ids)   # 每个工具调用都回流了结果
    # 结果落在 user 记录里(cc digest pass1 只从 user 消息收 tool_result)
    result_contents = [b.get("content") for r in recs
                       for b in (r.get("message", {}).get("content") or [])
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert any("skill_content" in (c or "") for c in result_contents)


def test_digest_from_structured_records(tmp_path):
    recs = load_structured_records(_write(tmp_path, FRIENDLY))
    digest = _digest_from_cc_records(recs)
    assert digest.skill_used == "ascendc-crash-debug"
    trace_names = [t.name for t in digest.tool_trace]
    assert "Skill" in trace_names and "Read" in trace_names
    # read 结果回流(build_digest 会把路径按公共根相对化,故验内容不验原路径)
    assert any("import os" in f.content for f in digest.input_files)


def test_slice_finds_skill_segment(tmp_path):
    from skill_eval.audit.slice import slice_transcript_by_skill
    recs = load_structured_records(_write(tmp_path, FRIENDLY))
    jsonl = tmp_path / "expanded.jsonl"
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    segs = slice_transcript_by_skill(jsonl, "ascendc-crash-debug")
    assert len(segs) >= 1    # skill 被调用过 → 切得出段
