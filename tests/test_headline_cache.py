"""headline_cache 单元测试:per-declaration 人话标题的磁盘缓存。

纯函数(monkeypatch _cache_dir → tmp_path,绝不污染 ~/.cache)。镜像 refine_cache 的
测试口径:键含 source_excerpt(headline 依赖它)、put/get 往返、get_or_compute 命中不重算。
"""

from __future__ import annotations

from sift.audit import headline_cache
from sift.models import (
    CheckMethod,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
)


def _inst(excerpt: str = "design_doc_cases.json 必须存在") -> InstructionCheck:
    return InstructionCheck(
        source=InstructionSource.CONSTRAINT,
        instruction_text="必须存在",
        section="Constraint",
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        source_excerpt=excerpt,
    )


def test_cache_key_includes_source_excerpt(monkeypatch, tmp_path):
    """source_excerpt 进键:excerpt 变 → key 变(headline 依赖 excerpt,改了应重算)。"""
    monkeypatch.setattr(headline_cache, "_cache_dir", lambda: tmp_path)
    a = headline_cache.cache_key([_inst(excerpt="A 必须存在")], None)
    b = headline_cache.cache_key([_inst(excerpt="B 必须存在")], None)
    assert a != b
    # 同 excerpt → 同 key(稳定、确定性)
    assert a == headline_cache.cache_key([_inst(excerpt="A 必须存在")], None)


def test_put_get_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(headline_cache, "_cache_dir", lambda: tmp_path)
    key = "k1"
    assert headline_cache.get(key) is None  # miss
    headline_cache.put(key, ["标题一", "标题二"])
    assert headline_cache.get(key) == ["标题一", "标题二"]


def test_get_or_compute_caches_and_skips_on_hit(monkeypatch, tmp_path):
    """miss 调 compute_fn 并落盘;hit 复用、不再调 compute_fn。"""
    monkeypatch.setattr(headline_cache, "_cache_dir", lambda: tmp_path)
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return ["h1"]

    key = headline_cache.cache_key([_inst()], None)
    assert headline_cache.get_or_compute(key, compute) == ["h1"]
    assert headline_cache.get_or_compute(key, compute) == ["h1"]  # 命中,不重算
    assert calls["n"] == 1
