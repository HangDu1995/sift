"""Run-directory path semantics: parse ``run-N[-retry-K]`` names and index the
**final (highest-retry)** run directory per ``(eval_name, run_n)``.

Shared by ``comparison_suite`` (pairing with/without outputs) and ``benchmark``
(per-run aggregation). Both must treat one "run" as the final attempt of
``(eval, run_number)``: ``glob("run-*")`` / ``glob("eval-*/run-*")`` also match
``run-N-retry-K`` directories, so naive glob+iterate double-counts superseded
failed attempts — dragging the aggregated mean toward the failed base runs and
inflating run counts. Indexing by ``(eval_name, run_n)`` and keeping the
highest retry yields exactly one entry per logical run (its final attempt).
"""

from __future__ import annotations

import re
from pathlib import Path

_RUN_RE = re.compile(r"^run-(\d+)(?:-retry-(\d+))?$")


def retry_count(name: str) -> int:
    """Retry 计数 (``run-1`` → 0, ``run-1-retry-2`` → 2);非 run 目录 → -1。"""
    m = _RUN_RE.match(name)
    if not m:
        return -1
    return int(m.group(2)) if m.group(2) else 0


def is_nested(config_dir: Path) -> bool:
    """该 config 目录(with_skill/without_skill)是否含 ``eval-*`` 分层。"""
    return any(d.is_dir() for d in config_dir.glob("eval-*"))


def index_runs(config_dir: Path, nested: bool) -> dict[tuple[str | None, int], Path]:
    """按 ``(eval_name, run_n)`` 索引一侧 run 目录到**最终 retry** 目录。

    nested=True  → ``config_dir/eval-{name}/run-{n}[-retry-{k}]``
    nested=False → ``config_dir/run-{n}[-retry-{k}]`` (eval_name=None 单桶)
    同一 ``(eval_name, run_n)`` 有基 + 多 retry 时,保留 retry 计数最大(= 最终尝试)。
    返回 dict 按 key 排序(确定序),不匹配 run 命名的目录被忽略。
    """
    indexed: dict[tuple[str | None, int], Path] = {}
    if nested:
        parents = [(d.name, d) for d in sorted(config_dir.glob("eval-*"), key=lambda p: p.name) if d.is_dir()]
    else:
        parents = [(None, config_dir)]
    for eval_name, parent in parents:
        for run_dir in sorted(parent.glob("run-*"), key=lambda p: p.name):
            if not run_dir.is_dir():
                continue
            m = _RUN_RE.match(run_dir.name)
            if not m:
                continue
            run_n = int(m.group(1))
            key = (eval_name, run_n)
            existing = indexed.get(key)
            if existing is None or retry_count(run_dir.name) > retry_count(existing.name):
                indexed[key] = run_dir
    return {k: indexed[k] for k in sorted(indexed)}
