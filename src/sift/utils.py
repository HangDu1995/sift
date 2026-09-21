"""Usage accounting and model-call re-exports."""

from __future__ import annotations

import threading
from collections.abc import Callable

from sift.llm_transport import LLMCallConfig as LLMCallConfig
from sift.llm_transport import call_claude as call_claude
from sift.llm_transport import claude_cli_env as claude_cli_env


class TokenUsageAccumulator:
    """线程安全地累加 call_claude 的 token 用量。

    audit 判官在线程池里并发触发 ``on_usage``(=``self.add``),snapshot 在 audit
    末尾单线程取。锁防并发丢失更新(实证见 tests/test_audit_parallel.py)。
    """

    _KEYS = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_cost_usd",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {k: 0 for k in self._KEYS}
        self._counts["calls"] = 0
        # 按阶段(refine/headline/judge/…)分桶 token,供报告分阶段展示。无 stage 的 add
        # 不进这里(只进总桶 _counts)。lazily 建。
        self._by_stage: dict[str, dict] = {}

    def add(self, payload: dict, *, count: int = 1, stage: str | None = None) -> None:
        """on_usage 回调目标 / rollup 子快照累加。缺键补 0(不 KeyError)。
        count: 本次 add 计几次调用;默认 1(单次 call_claude 回调)。rollup 子快照时传
        count=snap.get("calls",1) 以保留子内部真实调用数(否则 add 一次会吞掉 child 的 calls)。
        stage: 归属的 audit 阶段(如 "refine"/"judge");给定时同时累加到该阶段桶,
            便于报告分阶段展示 token。省略 → 只进总桶(向后兼容老调用方)。
        """
        with self._lock:
            for k in self._KEYS:
                v = payload.get(k, 0) or 0
                self._counts[k] += v
                if not (stage is not None):
                    continue
                self._stage_counts(stage)[k] += v
            self._counts["calls"] += count
            if stage is not None:
                self._stage_counts(stage)["calls"] += count

    def scoped(self, stage: str) -> Callable[[dict], None]:
        """返绑定 stage 的 add 闭包,供 ``on_usage=acc.scoped("refine")`` 把该处
        call_claude 的 token 归到指定阶段。线程安全(复用 add 的锁)。
        """

        def _add(payload: dict, count: int = 1) -> None:
            self.add(payload, stage=stage, count=count)

        return _add

    def snapshot_by_stage(self) -> dict[str, dict]:
        """返各阶段 token 桶的副本 {stage: {input_tokens, …, calls}}。无任何带 stage
        的 add → 空 dict。只含显式归阶段的量;总桶用 snapshot()。
        """
        with self._lock:
            return {stage: dict(sc) for stage, sc in self._by_stage.items()}

    def snapshot(self) -> dict:
        """返当前累计的副本;无任何 usage(所有 token 字段 + calls 皆 0)返空 dict。
        判空看 token 不只看 calls:executor 段 count=0 但有 token(executor 非评分 LLM 调用)仍应非空。
        """
        with self._lock:
            if not any(self._counts.values()):
                return {}
            return dict(self._counts)

    def _stage_counts(self, stage: str) -> dict:
        sc = self._by_stage.get(stage)
        if sc is None:
            sc = {k: 0 for k in self._KEYS}
            sc["calls"] = 0
            self._by_stage[stage] = sc
        return sc


class DurationAccumulator:
    """线程安全累加时长(秒)。rollup 各类调用的 perf_counter 时长(scoring/audit)。

    与 TokenUsageAccumulator 不同:time 是墙钟、单值,只累加秒数(无 calls/cost 多字段)。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0.0

    def add(self, seconds: float | None) -> None:
        with self._lock:
            self._total += seconds or 0.0

    def snapshot(self) -> float:
        with self._lock:
            return self._total
