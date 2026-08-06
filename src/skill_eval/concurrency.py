"""自适应并发限制器 + AIMD 控制器。

替代固定 ``threading.Semaphore``,动态调在飞数上限:撞限流(429)乘性减、连续成功
加性增(AIMD),收敛到当前后端的真实容量边界。

**为什么对并发限制 + 速率限制两类后端都通用**:控在飞数 = 控吞吐
(``吞吐 = 在飞数 ÷ 单请求时长``)。AIMD 收敛到"不撞限的最大在飞数":
并发限制后端 → 并发容量;速率限制后端 → ``速率限 × 单请求时长``(吞吐逼近速率限
= 该后端真实上限)。不假设限制类型、只对限流信号反应,故适配任意后端。
"""
from __future__ import annotations

import threading
import time


class AdaptiveConcurrencyLimiter:
    """可动态调上限的并发限制器 + AIMD 控制器。

    - acquire/release/context manager:语义同 ``threading.Semaphore``(在飞数 ≤ ceiling)。
    - on_success():加性增(连续 ``probe_every`` 次 → ceiling+1,封顶 cap)。
    - on_throttled():乘性减(ceiling//=2,不破 floor)。
    - set_fixed(n):锁死 ceiling=n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 向后兼容)。
    - set_adaptive():恢复 AIMD(默认模式)。
    - reset(initial):还原(测试用)。

    线程安全:所有状态读写均在 ``_cond`` 锁下。降 ceiling 不强杀在飞请求(等其 release
    自然减 ``_in_flight``,降到 < ceiling 才放行新请求);升 ceiling ``notify_all`` 唤醒等待者。
    """

    def __init__(self, initial: int, floor: int = 2, cap: int = 32, probe_every: int = 8):
        if initial < 1:
            raise ValueError(f"initial 必须 ≥ 1,收到 {initial}")
        self._cond = threading.Condition()
        self._in_flight = 0
        self._ceiling = initial
        self._floor = max(1, floor)
        self._cap = max(initial, cap)
        self._probe_every = max(1, probe_every)
        self._streak = 0
        self._adaptive = True

    # ── 并发门控(同 Semaphore 语义)────────────────────────────────────────
    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """获取一个在飞名额。``blocking=False`` 满则立即返 ``False``(不阻塞);
        ``blocking=True`` 阻塞到有名额,``timeout`` 给定则超时返 ``False``。"""
        with self._cond:
            if not blocking:
                if self._in_flight >= self._ceiling:
                    return False
            else:
                end = None if timeout is None else time.monotonic() + timeout
                while self._in_flight >= self._ceiling:
                    if end is None:
                        self._cond.wait()
                    else:
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            return False
                        self._cond.wait(remaining)
            self._in_flight += 1
            return True

    def release(self) -> None:
        """释放一个在飞名额。降到 ``_in_flight < _ceiling`` 则 notify 等待者。"""
        with self._cond:
            self._in_flight -= 1
            if self._in_flight < self._ceiling:
                self._cond.notify_all()

    def __enter__(self) -> "AdaptiveConcurrencyLimiter":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ── AIMD 调节 ──────────────────────────────────────────────────────────
    def on_success(self) -> None:
        """成功一次:streak+1,达 ``probe_every`` 则加性增(ceiling+1 封顶 cap)。
        升 ceiling 后 ``notify_all`` 唤醒等待者。固定模式 no-op。"""
        with self._cond:
            if not self._adaptive:
                return
            self._streak += 1
            if self._streak >= self._probe_every:
                old = self._ceiling
                self._ceiling = min(self._cap, self._ceiling + 1)
                self._streak = 0
                if self._ceiling > old:
                    self._cond.notify_all()

    def on_throttled(self) -> None:
        """撞限流:乘性减(ceiling//=2 不破 floor)、streak 清零。固定模式 no-op。
        降 ceiling **不 notify**:_in_flight 可能 > 新 ceiling,等其 release 自然减后
        才放行(否则 notify 会让等待者立即通过 → 瞬时超新 ceiling)。"""
        with self._cond:
            if not self._adaptive:
                return
            self._ceiling = max(self._floor, self._ceiling // 2)
            self._streak = 0

    # ── 模式切换 ───────────────────────────────────────────────────────────
    def set_fixed(self, n: int) -> None:
        """锁死 ceiling=n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 固定向后兼容)。"""
        if n < 1:
            raise ValueError(f"固定并发必须 ≥ 1,收到 {n}")
        with self._cond:
            self._adaptive = False
            self._ceiling = n
            self._streak = 0
            self._cond.notify_all()  # 升/降都可能让等待者通过,唤醒重检

    def set_adaptive(self, initial: int | None = None) -> None:
        """恢复 AIMD 自适应(默认模式)。``initial`` 给则重置起点,否则保留当前 ceiling。"""
        with self._cond:
            self._adaptive = True
            if initial is not None:
                self._ceiling = initial
            self._streak = 0
            self._cond.notify_all()

    def reset(self, initial: int) -> None:
        """还原到初始状态(测试用:清 in_flight、ceiling=initial、streak=0、adaptive=True)。"""
        with self._cond:
            self._adaptive = True
            self._ceiling = initial
            self._streak = 0
            self._in_flight = 0
            self._cond.notify_all()

    # ── 只读视图(测试/诊断)──────────────────────────────────────────────
    @property
    def ceiling(self) -> int:
        with self._cond:
            return self._ceiling

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def adaptive(self) -> bool:
        with self._cond:
            return self._adaptive


# ── 模块级单例 + 配置(audit 与 run 侧 grading/comparator/analyzer 共享一个 ceiling)────
# 全局自适应并发限制器:_judge_batch_with_retry / call_llm_with_retry 在 call_claude 前后
# acquire/release,把跨所有调用方(判定器批 + run 侧单次判定)的在飞 LLM 调用钉在 ≤ ceiling。
# AIMD 调 ceiling:撞 429(_HttpRateLimited)乘性减、连续成功加性增,收敛到后端真实容量边界。
# 模块级单例 → 跨调用共享(端点容量稳定),audit 与 run 侧争同一个(同账号同容量预算)。
_MAX_BATCH_CONCURRENT = 6  # AIMD 探测起点(保守);真实边界由 on_throttled/on_success 摸出
_LLM_LIMITER = AdaptiveConcurrencyLimiter(_MAX_BATCH_CONCURRENT)
# 向后兼容别名:旧引用/测试按 _LLM_SEMAPHORE 名访问(limiter 兼容 acquire/release/__enter__)。
_LLM_SEMAPHORE = _LLM_LIMITER
# AIMD 退避:撞 429 后 sleep 这么久(+ random 抖动防多线程同步重试)再重试。
_RATE_LIMIT_BACKOFF = 2.0


def configure_max_concurrent(n: int) -> None:
    """锁死全局并发上限 = n、关 AIMD(= 旧行为,供 ``--max-concurrent`` 固定向后兼容)。

    供 CLI ``--max-concurrent`` / env ``SKILL_EVAL_MAX_CONCURRENT`` 在大声明、低内存机器、
    或多 audit 共存场景下调小(用时间换内存)。在 audit/run 起判定前调。
    """
    if n < 1:
        raise ValueError(f"max_concurrent 必须 ≥ 1,收到 {n}")
    _LLM_LIMITER.set_fixed(n)


def configure_adaptive(initial: int | None = None) -> None:
    """恢复 AIMD 自适应并发(默认模式)。``initial`` 给则重设探测起点,否则保留当前 ceiling。

    通用工具默认自适应——动态探当前后端容量边界(严苛后端收敛到低值、宽松后端探到更高),
    不写死固定值。
    """
    _LLM_LIMITER.set_adaptive(initial)

