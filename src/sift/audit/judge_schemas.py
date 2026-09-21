"""批量审计判官使用的结构化输出格式。"""


# ── 结构化输出 schema:判官走 --json-schema,源头约束格式不可畸形 ───────────────
# CLI 不接受顶层 array schema(spike 实测触发 error_max_structured_output_retries),
# 故 array 判官把输出包成 {"items":[...]};call_claude 透明解开(items)→ judge 拿纯
# array、parser 零改动(extract_first_balanced/load_json_lenient 照常命中,fallback
# 留作 structured_output 缺失时兜底)。required 字段对齐各 prompt「输出格式」段;
# indeterminate/turn_refs/reason 选填(parser 容错缺键)。verdict 用 enum。
def _array_schema(item_schema: dict) -> dict:
    """array 判官 schema:顶层 object 包 items(规避 CLI 顶层 array 限制)。"""
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": item_schema}},
        "required": ["items"],
    }


APPLICABLE_SCHEMA = _array_schema(
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "applicable": {"type": "boolean"},
            "indeterminate": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["index", "applicable"],
    }
)
CLASSIFY_REAL_SCHEMA = _array_schema(
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "is_real": {"type": "boolean"},
            "indeterminate": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["index", "is_real"],
    }
)
HEADLINE_SCHEMA = _array_schema(
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "headline": {"type": "string"},
        },
        "required": ["index", "headline"],
    }
)
PROHIBITION_SCHEMA = _array_schema(
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "violated": {"type": "boolean"},
            "indeterminate": {"type": "boolean"},
            "evidence": {"type": "string"},
            "turn_refs": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["index", "violated", "evidence"],
    }
)
STEP_SCHEMA = _array_schema(
    {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "verdict": {
                "type": "string",
                "enum": ["pass", "fail", "na", "indeterminate"],
            },
            "evidence": {"type": "string"},
            "turn_refs": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["index", "verdict", "evidence"],
    }
)
