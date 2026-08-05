# tests/test_concurrency.py
"""AdaptiveConcurrencyLimiter 单元测:基础门控 + AIMD 调节 + 动态 ceiling 并发边界。

替代固定 threading.Semaphore,动态调在飞数上限,撞限流乘性减、连续成功加性增。
"""
from __future__ import annotations

import json
import threading

import pytest

from skill_eval.audit import auditor
from skill_eval.audit.concurrency import AdaptiveConcurrencyLimiter
from skill_eval.utils import _HttpRateLimited


# ── 基础门控(Semaphore 语义)─────────────────────────────────────────────


def test_acquire_up_to_ceiling_then_nonblocking_fails():
    lim = AdaptiveConcurrencyLimiter(initial=3)
    assert lim.acquire(blocking=False)
    assert lim.acquire(blocking=False)
    assert lim.acquire(blocking=False)
    assert lim.in_flight == 3
    assert lim.acquire(blocking=False) is False  # 满


def test_release_frees_slot():
    lim = AdaptiveConcurrencyLimiter(initial=2)
    lim.acquire()
    lim.acquire()
    assert lim.acquire(blocking=False) is False
    lim.release()
    assert lim.acquire(blocking=False) is True


def test_context_manager_releases_on_exit():
    lim = AdaptiveConcurrencyLimiter(initial=2)
    with lim:
        assert lim.in_flight == 1
    assert lim.in_flight == 0
    assert lim.acquire(blocking=False) is True  # 退出后名额归还


def test_context_manager_releases_on_exception():
    lim = AdaptiveConcurrencyLimiter(initial=1)
    with pytest.raises(RuntimeError):
        with lim:
            raise RuntimeError("boom")
    assert lim.in_flight == 0


def test_initial_validation():
    with pytest.raises(ValueError):
        AdaptiveConcurrencyLimiter(initial=0)


def test_blocking_acquire_times_out():
    lim = AdaptiveConcurrencyLimiter(initial=1)
    lim.acquire()  # 占满
    assert lim.acquire(blocking=True, timeout=0.1) is False  # 超时


# ── AIMD 调节 ──────────────────────────────────────────────────────────────


def test_on_success_additive_increase_after_probe_every():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=3)
    for _ in range(3):
        lim.on_success()
    assert lim.ceiling == 5  # +1


def test_on_success_streak_resets_after_increase():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=2)
    lim.on_success()
    lim.on_success()  # +1 → ceiling 5, streak reset
    assert lim.ceiling == 5
    lim.on_success()  # streak=1, 不够 probe_every
    assert lim.ceiling == 5  # 不变


def test_on_success_caps_at_cap():
    lim = AdaptiveConcurrencyLimiter(initial=4, cap=5, probe_every=1)
    lim.on_success()  # →5 (cap)
    assert lim.ceiling == 5
    lim.on_success()  # 封顶不超
    assert lim.ceiling == 5


def test_on_throttled_multiplicative_decrease():
    lim = AdaptiveConcurrencyLimiter(initial=8)
    lim.on_throttled()
    assert lim.ceiling == 4  # 8//2


def test_on_throttled_floored():
    lim = AdaptiveConcurrencyLimiter(initial=3, floor=2)
    lim.on_throttled()  # 3//2=1 → floor 2
    assert lim.ceiling == 2
    lim.on_throttled()  # 2//2=1 → floor 2
    assert lim.ceiling == 2


def test_fixed_mode_on_success_noop():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=1)
    lim.set_fixed(3)
    assert lim.adaptive is False
    for _ in range(10):
        lim.on_success()
    assert lim.ceiling == 3  # 固定,不变


def test_fixed_mode_on_throttled_noop():
    lim = AdaptiveConcurrencyLimiter(initial=4)
    lim.set_fixed(3)
    lim.on_throttled()
    assert lim.ceiling == 3


def test_set_adaptive_resumes():
    lim = AdaptiveConcurrencyLimiter(initial=4, probe_every=1)
    lim.set_fixed(3)
    lim.set_adaptive()
    assert lim.adaptive is True
    lim.on_success()
    assert lim.ceiling == 4  # 恢复后 +1


def test_set_fixed_validation():
    lim = AdaptiveConcurrencyLimiter(initial=4)
    with pytest.raises(ValueError):
        lim.set_fixed(0)


# ── 动态 ceiling 并发边界(关键正确性)──────────────────────────────────────


def test_blocking_acquire_waits_for_release():
    """acquire 阻塞到名额释放。"""
    lim = AdaptiveConcurrencyLimiter(initial=1)
    lim.acquire()  # 主线程占满
    acquired = threading.Event()

    def worker():
        lim.acquire()  # 阻塞
        acquired.set()

    t = threading.Thread(target=worker)
    t.start()
    assert not acquired.wait(0.1)  # 还在等
    lim.release()  # 释放 → worker 拿到
    assert acquired.wait(2.0)
    t.join()
    lim.release()  # worker 的名额


def test_on_throttled_lowering_keeps_inflight_blocked():
    """降 ceiling 后,in_flight 仍 >= 新 ceiling → 新 acquire 继续阻塞,
    直到在飞 release 自然减到 < ceiling 才放行(验证不强杀、不超限)。"""
    lim = AdaptiveConcurrencyLimiter(initial=4)
    for _ in range(4):  # 占满 4
        lim.acquire()
    lim.on_throttled()  # ceiling 4→2, in_flight 仍 4
    assert lim.ceiling == 2
    assert lim.acquire(blocking=False) is False  # 4 >= 2,拒绝
    lim.release()  # in_flight 3,仍 >= 2
    assert lim.acquire(blocking=False) is False
    lim.release()  # in_flight 2,仍 >= 2
    assert lim.acquire(blocking=False) is False
    lim.release()  # in_flight 1 < 2,放行
    assert lim.acquire(blocking=False) is True


def test_on_success_raising_wakes_waiter():
    """升 ceiling 后,等待的 acquire 被 notify 唤醒通过。"""
    lim = AdaptiveConcurrencyLimiter(initial=1, probe_every=1)
    lim.acquire()  # 占满(ceiling=1)
    got = threading.Event()

    def worker():
        lim.acquire()
        got.set()

    t = threading.Thread(target=worker)
    t.start()
    assert not got.wait(0.1)  # 阻塞(ceiling=1 满)
    lim.on_success()  # ceiling 1→2 → notify → worker 通过(仍有 1 名额)
    assert got.wait(2.0)
    t.join()
