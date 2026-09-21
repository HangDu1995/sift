"""records 加载时 stamp 全局 _turn_index;切片/序列化往返保留;opencode 重建 dict 不丢。"""

from __future__ import annotations

import json

from sift.evals.opencode import _records_from_opencode
from sift.evals.structured import load_structured_records
from sift.evals.transcript import _records_from_cc, stamp_turn_indices


def test_stamp_assigns_1based_global_index():
    recs = [{"role": "assistant", "text": "a"}, {"role": "user", "text": "b"}, {"role": "assistant", "text": "c"}]
    stamp_turn_indices(recs)
    assert [r["_turn_index"] for r in recs] == [1, 2, 3]


def test_stamp_idempotent_preserves_existing():
    """已有 _turn_index 不覆盖;未带的按 list 位置 stamp(第 2 条 → 2)。"""
    recs = [{"_turn_index": 142, "text": "a"}, {"text": "b"}]
    stamp_turn_indices(recs)
    assert recs[0]["_turn_index"] == 142  # 保留
    assert recs[1]["_turn_index"] == 2  # 位置 2(注:混合 list 生产中不出现,仅钉 idempotent)


def test_cc_jsonl_records_carry_turn_index(tmp_path):
    p = tmp_path / "s.jsonl"
    transcript_records = [
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}},
        {"message": {"role": "user", "content": [{"type": "text", "text": "y"}]}},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in transcript_records))
    recs = _records_from_cc(p)
    assert [r["_turn_index"] for r in recs] == [1, 2]


def test_structured_records_carry_turn_index(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "format": "sift-records",
                "records": [
                    {"role": "assistant", "text": "a"},
                    {"role": "assistant", "text": "b"},
                ],
            }
        )
    )
    recs = load_structured_records(p)
    assert [r["_turn_index"] for r in recs] == [1, 2]


def test_opencode_records_carry_turn_index(tmp_path):
    """opencode 路径 L157 dict 重建是字段丢失风险点:重建后的 records 必须带 _turn_index。

    直接调 _records_from_opencode(模块级,签名仅要 Path),最小诚实地覆盖重建路径。
    """
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "info": {"agent": "dev"},
                "messages": [
                    {"info": {"role": "assistant", "time": {"created": 1}}, "parts": [{"type": "text", "text": "a"}]},
                    {"info": {"role": "assistant", "time": {"created": 2}}, "parts": [{"type": "text", "text": "b"}]},
                ],
            }
        )
    )
    recs = _records_from_opencode(p)
    assert [r["_turn_index"] for r in recs] == [1, 2]


def test_opencode_records_carry_turn_index_with_tool_result(tmp_path):
    """一条消息含 tool part → 展开成 2 records(主消息 + tool_result user 消息),
    各按 out 位置 stamp(与 cc 同款:assistant 消息与随后的 tool_result 各占一位)。
    """
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {
                "info": {"agent": "dev"},
                "messages": [
                    {
                        "info": {"role": "assistant", "time": {"created": 1}},
                        "parts": [
                            {
                                "type": "tool",
                                "tool": "read",
                                "state": {"input": {"filePath": "a.txt"}, "output": "<path>a</path>"},
                            },
                        ],
                    },
                    {
                        "info": {"role": "assistant", "time": {"created": 2}},
                        "parts": [{"type": "text", "text": "done"}],
                    },
                ],
            }
        )
    )
    recs = _records_from_opencode(p)
    # 消息1 → 2 records(assistant tool_use + user tool_result),消息2 → 1 record
    assert len(recs) == 3
    assert [r["_turn_index"] for r in recs] == [1, 2, 3]
