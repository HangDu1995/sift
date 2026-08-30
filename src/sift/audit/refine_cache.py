"""refine_cache: per-SKILL refine 结果的磁盘缓存 + 并发锁。

``TranscriptAuditor._refine_pseudo_instructions`` 用 LLM 砍 extractor 误抽的示例/占位
伪指令,per-skill 调一次。但 ``runner.py`` 每 (eval×config×run) 调一次 ``audit()``,
同一 SKILL.md 跨 audit() 反复 refine(实测 3 skill 跑 10+ 分钟)。本模块缓存 refine
结果:同 instructions + model + prompt 版本只调一次 ``compute_fn``,跨 audit() 调用复用。

键设计:直接 hash refine 实际输入(``_cache_key(instructions, model)``)——SKILL.md 改了
无关部分(指令没变)仍命中。``REFINE_PROMPT_VERSION`` bump → 进键 → 强制失效所有旧条目
(``_llm_classify_real`` 的 prompt 改了就 bump)。

并发(ThreadPoolExecutor 多 audit() 并行):模块级 ``_REFINE_LOCK = threading.Lock()`` +
``get_or_compute`` 内部 double-check locking——拿锁后再查一次缓存,保证并发首次 miss
只算一次 ``compute_fn``。

序列化无损性(关键不变量):``InstructionCheck`` 是 pydantic BaseModel,
``model_dump(mode="json")`` 把枚举序列化成值,``model_validate`` 无损重建,缓存 hit
返回的指令集等价于直接 refine 的结果。

纯函数模块、中文 docstring(镜像 ``audit/dedup.py`` / ``audit/aggregate.py`` 的组织)。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from pydantic import ValidationError

from sift.models import InstructionCheck


# bump 当 ``_llm_classify_real`` 的 prompt 改了 → 进键 → 强制失效所有缓存条目。
# v2:prompt 喂 source_excerpt(判「命令行标识符伪工具」要看原文上下文)+ 伪指令类目扩义。
# v3:伪工具类目再补「任务编号词」(「`./run.sh` 跑 Task 25 用例」抽出的「跑 Task」——Task 不是工具)。
# v4:is_real=true 侧补「任务清单行是真指令」——v3 曾误砍同构任务行(仅分支编号不同
# 的同款行被当示例砍、其余分支同款都保留;同类不同判=精度破口,违反「宁可多抽、不漏真指令」)。
REFINE_PROMPT_VERSION = 4

# 模块级锁:ThreadPoolExecutor 并发 audit() 时,保证同 key 的 compute_fn 只调一次。
_REFINE_LOCK = threading.Lock()


def _cache_dir() -> Path:
    """缓存目录(默认 ``~/.cache/sift/refine/``)。

    返函数而非常量:测试用 monkeypatch 改写本函数指向 ``tmp_path``,绝不污染 ``~/.cache``。
    生产代码每次读 ``Path.home()``,不缓存(廉价、无陈旧风险)。
    """
    return Path.home() / ".cache" / "sift" / "refine"


def _cache_key(instructions: list[InstructionCheck], model: str | None) -> str:
    """内容感知缓存键 = refine 实际输入(instructions)+ model + prompt 版本的 sha256[:16]。

    payload 形如 ``"HEADING_STEP|Process|Step 1: 真步骤|摘录…"|<model>|v<REFINE_PROMPT_VERSION>"``
    (每条指令拼 ``source.value|section|instruction_text|source_excerpt``,``|`` 分隔;
    excerpt v2 起进 prompt 也进键——摘录变了 refine 结论可能变)。直接 hash refine
    输入——SKILL.md 改了无关部分(指令文本/来源/标题没变)仍命中。``model`` 进键(不同
    模型可能给出不同 refine 结论)。``REFINE_PROMPT_VERSION`` 进键(prompt 改了 → bump →
    强制失效)。

    确定性:同 instructions + model + version → 恒同 key(跨调用、跨进程稳定)。
    """
    payload = "|".join(
        f"{s.source.value}|{s.section}|{s.instruction_text}|{s.source_excerpt}"
        for s in instructions
    ) + f"|{model}|v{REFINE_PROMPT_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get(key: str) -> tuple[list[InstructionCheck], int] | None:
    """读盘 → ``(refined, dropped_count)``;miss(文件不存在 / 解析失败)→ None。

    miss 容错:文件不存在 / JSON 解析失败 / 缺键 / ``InstructionCheck`` 反序列化失败 →
    一律返 None(调用方重算,``put`` 覆盖)。幂等、无副作用(磁盘 I/O 异常不上抛)。
    """
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        refined = [InstructionCheck.model_validate(item) for item in data["refined"]]
        dropped_count = int(data["dropped_count"])
    except (
        json.JSONDecodeError,
        KeyError,
        ValueError,
        TypeError,
        OSError,
        ValidationError,
    ):
        return None
    return refined, dropped_count


def put(key: str, refined: list[InstructionCheck], dropped_count: int) -> None:
    """写盘(原子:写 ``<key>.json.tmp`` 再 ``os.replace`` 覆盖 ``<key>.json``)。

    幂等:同 key 多次 put 覆盖。``InstructionCheck`` 是 pydantic BaseModel,
    ``model_dump(mode="json")`` 把枚举序列化成值,``model_validate`` 无损重建。
    目录不存在时自动 ``mkdir(parents=True)``。
    """
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "refined": [inst.model_dump(mode="json") for inst in refined],
        "dropped_count": int(dropped_count),
    }
    final = cache_dir / f"{key}.json"
    tmp = cache_dir / f"{key}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def get_or_compute(
    key: str,
    compute_fn,
) -> tuple[list[InstructionCheck], int]:
    """miss → 调 ``compute_fn`` 算 + ``put`` 缓存;hit → 直接返缓存。

    并发安全(ThreadPoolExecutor 多线程):模块级 ``_REFINE_LOCK`` + double-check
    locking——fast path 不拿锁查一次(命中直接返,避免锁竞争);miss 时拿锁,**拿锁后
    再查一次**(另一线程可能刚 put),仍 miss 才调 ``compute_fn`` + ``put``。
    保证并发首次 miss 只算一次 ``compute_fn``。

    Args:
        key: ``_cache_key`` 算出的 sha256[:16]。
        compute_fn: ``() -> tuple[list[InstructionCheck], int]``,只在 miss 时调。
            ``(refined, dropped_count)`` = 精筛后的指令集 + 砍掉的条数。

    Returns:
        ``(refined, dropped_count)`` —— hit 来自缓存、miss 来自 compute_fn(已落盘)。
    """
    # Fast path:不拿锁查一次(命中直接返,避免锁竞争)。
    cached = get(key)
    if cached is not None:
        return cached
    with _REFINE_LOCK:
        # Double-check:拿锁后再查一次(另一线程可能刚 put)。
        cached = get(key)
        if cached is not None:
            return cached
        refined, dropped_count = compute_fn()
        put(key, refined, dropped_count)
        return refined, dropped_count
