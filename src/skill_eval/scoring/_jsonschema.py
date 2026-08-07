"""JSON schema 工具:把 pydantic model_json_schema() 产的 $ref/$defs 内联成无 $ref 副本。

为什么需要:forced tool_use 把 schema 当 tool input_schema 发给 LLM 端点。部分
Anthropic 兼容端点(如 GLM)**不解析 tool input_schema 里的 $ref**——当一个必填字段
或数组 item 是 $ref 时,模型看不到目标形状 → 直接返回 ``{}``(grader 实测 100%
返回 {},判定全 UNEVAL)。把 $ref 就地展开成 inline 后,模型总能看到完整字段形状。

内联 = 语义等价但无 $ref/$defs,对支持 $ref 的端点(原生 Anthropic)也无副作用。
"""

from __future__ import annotations


def inline_schema(schema: dict) -> dict:
    """把 schema 里所有 ``$ref`` 按 schema 自身 ``$defs`` 解开成 inline 副本。

    返回的新 schema 不含 ``$ref`` / ``$defs``。环引用断开成 ``{}``(这些模型无环,
    纯防御)。其他键(含 ``title`` 等)原样保留。
    """
    def _deref(node, seen: frozenset):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref in seen:
                    return {}  # 环:断开
                target = schema
                for part in ref.lstrip("#/").split("/"):
                    target = target[part]
                return _deref(target, seen | {ref})
            return {k: _deref(v, seen) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_deref(x, seen) for x in node]
        return node

    return _deref(schema, frozenset())
