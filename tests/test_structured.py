# Copyright (c) 2025-2026 Huawei Technologies Co., Ltd.
# 结构化 records 输入(reader)的单测:友好 JSON → cc 记录形 → 现有 digest/slice。
import json

from skill_eval.evals.structured import is_structured_records, load_structured_records
from skill_eval.evals.transcript import _digest_from_records


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
    digest = _digest_from_records(recs)
    assert digest.skill_used == "ascendc-crash-debug"
    trace_names = [t.name for t in digest.tool_trace]
    assert "Skill" in trace_names and "Read" in trace_names
    # read 结果回流(build_digest 会把路径按公共根相对化,故验内容不验原路径)
    assert any("import os" in f.content for f in digest.input_files)


def test_digest_from_structured_records_without_result(tmp_path):
    # insight 的 audit 桥接(buildStructuredRecords)为省 payload **不回传 tool 结果**
    # (resultJson 常达 10MB+,audit 判定不读——auditor 只取 user_turns/tool_trace/transcript_text,
    # derive_declarations 只读 input_files 的 path)。守卫:缺 result 的结构化 records 仍能产出
    # audit 真正消费的字段——skill_used / tool_trace / input_files 的 **path**(Read 的 file_path
    # 输入恢复,非靠 result)。仅文件内容空,audit 不读内容、无碍。
    no_result = {
        "format": "skill-eval-records",
        "records": [
            {"role": "user", "text": "分析 x.py 的崩溃"},
            {"role": "assistant", "agent": "kernel-developer", "parent_tool_use_id": None,
             "text": "调用 skill 排查",
             "tool_calls": [
                 {"name": "skill", "input": {"name": "ascendc-crash-debug"}},
                 {"name": "read", "input": {"file_path": "src/x.py"}},
             ]},
        ],
    }
    digest = _digest_from_records(load_structured_records(_write(tmp_path, no_result)))
    assert digest.skill_used == "ascendc-crash-debug"               # scene / by-skill 分段用
    assert [t.name for t in digest.tool_trace] == ["Skill", "Read"]  # tool_trace 用
    assert [f.path for f in digest.input_files] == ["x.py"]          # derive_declarations 读 path
    assert all(f.content == "" for f in digest.input_files)          # 内容空(无 result),audit 不读


def test_slice_finds_skill_segment(tmp_path):
    from skill_eval.audit.slice import slice_transcript_by_skill
    recs = load_structured_records(_write(tmp_path, FRIENDLY))
    jsonl = tmp_path / "expanded.jsonl"
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    segs = slice_transcript_by_skill(jsonl, "ascendc-crash-debug")
    assert len(segs) >= 1    # skill 被调用过 → 切得出段


def test_digest_cleans_opencode_format_read_result(tmp_path):
    # insight 实存形态:read 结果是 opencode 格式(<path>/<type>/<content> 包裹 + "N: " 行号),
    # 非 cc 的 "\d+\t"。共享重建器应按 <content> 标记识别并清洗,否则 input_files 内容被
    # XML 包裹 + 行号污染(且后续 Edit 的 old_string 匹配不上 → 假 unreconstructed / 假 FAIL)。
    rec = {
        "format": "skill-eval-records",
        "records": [
            {"role": "user", "text": "读 x.py"},
            {"role": "assistant", "parent_tool_use_id": None, "tool_calls": [
                {"name": "read", "input": {"file_path": "src/x.py"},
                 "result": ("<path>/x.py</path>\n<type>file</type>\n<content>\n"
                            "1: import os\n2: crash()\n</content>")},
            ]},
        ],
    }
    digest = _digest_from_records(load_structured_records(_write(tmp_path, rec)))
    assert digest.input_files, "read 应产出 input_file"
    content = digest.input_files[0].content
    assert "<content>" not in content and "<path>" not in content and "<type>" not in content
    assert "1: " not in content and "2: " not in content        # opencode N: 行号剥掉
    assert "import os" in content and "crash()" in content      # 真内容保留


def test_cc_format_read_not_overcleaned(tmp_path):
    # 无 <content> 包裹 → 走 cc 清洗(只剥 \d+\t 行号);正文里合法的 "N: ..." 文本不被当
    # opencode 行号误删(format-detection 守住 cc 不回归)。锁"清洗按 <content> 标记触发"。
    rec = {
        "format": "skill-eval-records",
        "records": [
            {"role": "assistant", "parent_tool_use_id": None, "tool_calls": [
                {"name": "read", "input": {"file_path": "a.md"},
                 "result": "1\tnote: 1: stays\n2\t2: also stays"},
            ]},
        ],
    }
    digest = _digest_from_records(load_structured_records(_write(tmp_path, rec)))
    content = digest.input_files[0].content
    assert "1: stays" in content and "2: also stays" in content   # 冒号文本没被误删

