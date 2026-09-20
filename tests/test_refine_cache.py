"""tests for sift.audit.refine_cache.

per-SKILL hash 缓存:同 instructions + model + prompt 版本只调一次 compute_fn,
跨 audit() 调用复用(ThreadPoolExecutor 多线程并发 → 模块级 Lock + double-check)。

缓存目录隔离由 ``conftest._isolate_refine_cache_dir`` 提供(全局 autouse),
绝不污染 ``~/.cache``。每个测试从空缓存开始(function-scoped tmp_path)。
"""

from __future__ import annotations

import threading
import time

import pytest

from sift.audit import refine_cache
from sift.models import InstructionCheck
from sift.models.enums import (
    CheckMethod,
    InstructionPriority,
    InstructionSource,
)


def _inst(
    text: str = "Step 1: 真步骤",
    source: InstructionSource = InstructionSource.HEADING_STEP,
    section: str = "Process",
) -> InstructionCheck:
    """造一条 InstructionCheck(默认 HEADING_STEP,可改 source/section)。"""
    return InstructionCheck(
        source=source,
        instruction_text=text,
        section=section,
        priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE,
        params={},
    )


class TestCacheMissAndHit:
    @staticmethod
    def test_get_miss_returns_none():
        """空缓存:get 任意 key 返 None。"""
        assert refine_cache.get("nonexistent_key") is None

    @staticmethod
    def test_put_then_get_hit():
        """put 后 get 命中,返等价 refined + dropped_count(对象不同、字段同)。"""
        refined = [_inst("Step 1: 真步骤"), _inst("Step 2: 又一步")]
        refine_cache.put("k1", refined, dropped_count=3)
        result = refine_cache.get("k1")
        assert result is not None
        got_refined, got_dropped = result
        assert got_dropped == 3
        assert len(got_refined) == 2
        # 等价(缓存重建后是不同对象,但 pydantic 字段全同)
        for orig, got in zip(refined, got_refined):
            assert got.source == orig.source
            assert got.section == orig.section
            assert got.instruction_text == orig.instruction_text
            assert got.priority == orig.priority
            assert got.check_method == orig.check_method
            assert got.params == orig.params

    @staticmethod
    def test_get_corrupt_json_returns_none(tmp_path):
        """文件存在但 JSON 解析失败 → None(容错,调用方重算)。"""
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        assert refine_cache.get("bad") is None

    @staticmethod
    def test_get_missing_dropped_count_returns_none(tmp_path):
        """JSON 缺 dropped_count 键 → None(键缺失视为坏文件)。"""
        (tmp_path / "incomplete.json").write_text('{"refined": []}', encoding="utf-8")
        assert refine_cache.get("incomplete") is None

    @staticmethod
    def test_put_atomic_uses_tmp_then_replace(tmp_path):
        """put 后 .tmp 文件不留(原子写:写 .tmp 再 os.replace)。"""
        refine_cache.put("atomic_key", [_inst("a")], 0)
        assert (tmp_path / "atomic_key.json").exists()
        assert not (tmp_path / "atomic_key.json.tmp").exists()


class TestCacheKey:
    @staticmethod
    def test_deterministic():
        """同 instructions + model + version → 同 key(进程间、跨调用稳定)。"""
        insts = [_inst("a"), _inst("b")]
        k1 = refine_cache.cache_key(insts, None)
        k2 = refine_cache.cache_key(list(insts), None)  # 不同 list 实例
        assert k1 == k2

    @staticmethod
    def test_changes_with_model():
        """model 变 → key 变(None vs "sonnet")。"""
        insts = [_inst("a")]
        assert refine_cache.cache_key(insts, None) != refine_cache.cache_key(insts, "sonnet")

    @staticmethod
    def test_changes_with_prompt_version(monkeypatch):
        """REFINE_PROMPT_VERSION bump → key 变(强制失效所有旧条目)。"""
        insts = [_inst("a")]
        k1 = refine_cache.cache_key(insts, None)
        monkeypatch.setattr(refine_cache, "REFINE_PROMPT_VERSION", refine_cache.REFINE_PROMPT_VERSION + 1)
        k2 = refine_cache.cache_key(insts, None)
        assert k1 != k2

    @staticmethod
    def test_changes_with_instruction_text():
        """任一 instruction_text 变 → key 变。"""
        assert refine_cache.cache_key([_inst("a")], None) != refine_cache.cache_key([_inst("b")], None)

    @staticmethod
    def test_changes_with_instruction_source():
        """任一 source 变 → key 变。"""
        a = _inst("a", source=InstructionSource.HEADING_STEP)
        b = _inst("a", source=InstructionSource.CONSTRAINT)
        assert refine_cache.cache_key([a], None) != refine_cache.cache_key([b], None)

    @staticmethod
    def test_changes_with_instruction_section():
        """任一 section 变 → key 变。"""
        a = _inst("a", section="Process")
        b = _inst("a", section="Other")
        assert refine_cache.cache_key([a], None) != refine_cache.cache_key([b], None)


class TestCacheDir:
    @staticmethod
    def test_put_writes_to_patched_dir(tmp_path):
        """put 写到 _cache_dir()(不污染 ~/.cache。"""
        refine_cache.put("mykey", [_inst("a")], 0)
        assert (tmp_path / "mykey.json").exists()

    @staticmethod
    def test_get_reads_from_patched_dir(tmp_path):
        """get 从 _cache_dir() 读。"""
        (tmp_path / "readkey.json").write_text('{"refined": [], "dropped_count": 0}', encoding="utf-8")
        result = refine_cache.get("readkey")
        assert result == ([], 0)

    @staticmethod
    def test_put_creates_nested_cache_dir(tmp_path, monkeypatch):
        """_cache_dir() 不存在时 put 自动 mkdir(parents=True)。"""
        nested = tmp_path / "nested" / "deep"
        monkeypatch.setattr(refine_cache, "_cache_dir", lambda: nested)
        refine_cache.put("k", [_inst("a")], 0)
        assert (nested / "k.json").exists()


class TestGetOrCompute:
    @staticmethod
    def test_miss_then_computes_and_caches():
        """首次 miss → 调 compute_fn + put;二次同 key → hit,不调 compute_fn。"""
        calls = {"n": 0}

        def compute():
            calls["n"] += 1
            return [_inst("a")], 0

        refined, dropped = refine_cache.get_or_compute("fresh_key", compute)
        assert calls["n"] == 1
        assert len(refined) == 1
        assert dropped == 0
        # 二次同 key:hit,不调 compute_fn
        refined2, dropped2 = refine_cache.get_or_compute("fresh_key", compute)
        assert calls["n"] == 1
        assert len(refined2) == 1
        assert dropped2 == 0

    @staticmethod
    def test_returns_cached_equivalent_to_computed():
        """hit 返回的 instructions 必须等价于直接 compute 的结果(无损序列化)。"""
        original = [
            _inst("Step 1: 真步骤"),
            _inst("禁止编造", source=InstructionSource.CONSTRAINT, section="Limits"),
        ]
        refine_cache.get_or_compute("equiv_key", lambda: (list(original), 2))
        # 二次 hit:读盘重建,字段应与 original 一致
        got_refined, got_dropped = refine_cache.get_or_compute(
            "equiv_key", lambda: (pytest.fail("should be cache hit"), 0)
        )
        assert got_dropped == 2
        assert len(got_refined) == len(original)
        for orig, got in zip(original, got_refined):
            assert got.source == orig.source
            assert got.section == orig.section
            assert got.instruction_text == orig.instruction_text
            assert got.priority == orig.priority
            assert got.check_method == orig.check_method
            assert got.params == orig.params

    @staticmethod
    def test_concurrent_same_key_compute_fn_once():
        """两线程同 key 并发调 get_or_compute → compute_fn 只调一次(double-check locking)。

        用拉长 compute_fn 时延(0.1s sleep)+ 两个线程同时启动,模拟 ThreadPoolExecutor
        下两个 audit() 同时 miss 同一 SKILL.md 缓存的场景。
        """
        calls = {"n": 0}
        lock = threading.Lock()

        def compute():
            with lock:
                calls["n"] += 1
            time.sleep(0.1)  # 拉长持锁窗口,让另一线程在线程 A 持锁期间到达
            return [_inst("a")], 0

        results = [None, None]

        def worker(i):
            results[i] = refine_cache.get_or_compute("same_key", compute)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert calls["n"] == 1  # double-check locking:只算一次
        assert results[0] is not None and results[1] is not None
        assert len(results[0][0]) == 1 and len(results[1][0]) == 1
        assert results[0][1] == 0 and results[1][1] == 0

    @staticmethod
    def test_distinct_keys_both_compute():
        """不同 key → 都算(无相互命中)。"""
        calls = {"a": 0, "b": 0}

        def make_compute(label):
            def compute():
                calls[label] += 1
                return [_inst(label)], 0

            return compute

        refine_cache.get_or_compute("key_a", make_compute("a"))
        refine_cache.get_or_compute("key_b", make_compute("b"))
        assert calls == {"a": 1, "b": 1}
