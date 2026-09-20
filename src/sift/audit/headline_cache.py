"""headline_cache: per-SKILL headline(人话标题)生成的磁盘缓存 + 并发锁。

``TranscriptAuditor._generate_headlines`` 用 LLM 把每条指令的 source_excerpt(原文 bullet)
改写成去噪、补主语、一句顺口的人话标题,供显示优先取用。per-declaration 调一次、跨
audit() 复用(同 refine 的动机:``runner.py`` 每 eval×config×run 调 audit(),同一份声明
反复生成 headline 是浪费)。

**判定零影响**:headline 是纯显示字段,judge 吃 instruction_text 不读它。本缓存只省
headline 生成的重复 LLM 调用,不碰判定链。

键设计:hash instructions 的 ``(source|section|instruction_text|source_excerpt)`` + model +
``HEADLINE_PROMPT_VERSION``——source_excerpt 变了(extract 改)或 prompt 改了(bump version)
→ 进键 → 强制失效。镜像 refine_cache 的并发安全(模块级锁 + double-check locking)。

序列化:headline 是 ``list[str]``,JSON 原生无损(无需 pydantic往返)。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from sift.models import InstructionCheck

# bump 当 _generate_headlines 的 prompt 改了 → 进键 → 强制失效所有缓存条目。
HEADLINE_PROMPT_VERSION = 2

# 模块级锁:ThreadPoolExecutor 并发 audit() 时,保证同 key 的 compute_fn 只调一次。
_HEADLINE_LOCK = threading.Lock()


def _cache_dir() -> Path:
    """缓存目录(默认 ``~/.cache/sift/headline/``)。

    返函数而非常量:测试用 monkeypatch 改写本函数指向 ``tmp_path``,绝不污染 ``~/.cache``。
    """
    return Path.home() / ".cache" / "sift" / "headline"


def cache_key(instructions: list[InstructionCheck], model: str | None) -> str:
    """内容感知缓存键 = headline 实际输入(instructions,含 source_excerpt)+ model + 版本。

    payload 每条指令拼 ``source.value|section|instruction_text|source_excerpt``(``|`` 分隔)
    + ``model`` + ``v<HEADLINE_PROMPT_VERSION>``。**source_excerpt 进键**:extract 改了
    bullet 回溯(→ source_excerpt 变)→ headline 应重算,故必须进键。
    """
    payload = (
        "|".join(f"{s.source.value}|{s.section}|{s.instruction_text}|{s.source_excerpt}" for s in instructions)
        + f"|{model}|v{HEADLINE_PROMPT_VERSION}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get(key: str) -> list[str] | None:
    """读盘 → headlines(list[str],按输入指令顺序);miss(文件不存在/解析失败)→ None。

    miss 容错:文件不存在 / JSON 解析失败 / 缺键 → 一律 None(调用方重算,put 覆盖)。
    """
    path = _cache_dir() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        headlines = [str(h) for h in data["headlines"]]
    except (KeyError, ValueError, TypeError, OSError):
        return None
    return headlines


def put(key: str, headlines: list[str]) -> None:
    """写盘(原子:写 ``<key>.json.tmp`` 再 ``os.replace`` 覆盖)。幂等。"""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"headlines": list(headlines)}
    final = cache_dir / f"{key}.json"
    tmp = cache_dir / f"{key}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def get_or_compute(key: str, compute_fn) -> list[str]:
    """miss → 调 ``compute_fn`` 算 + ``put`` 缓存;hit → 直接返缓存。

    并发安全:模块级 ``_HEADLINE_LOCK`` + double-check locking(fast path 不拿锁查一次;
    miss 时拿锁,拿锁后再查一次,仍 miss 才调 ``compute_fn``)。
    """
    cached = get(key)
    if cached is not None:
        return cached
    with _HEADLINE_LOCK:
        cached = get(key)
        if cached is not None:
            return cached
        headlines = compute_fn()
        put(key, headlines)
        return headlines


# Compatibility for callers importing the former private helper.
