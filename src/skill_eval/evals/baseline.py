"""Frozen baseline eval-set: snapshot copy + provenance meta sidecar."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import skill_eval
from skill_eval.evals.loader import EvalLoader


def freeze_baseline(
    evals_path: Path,
    name: str = "baseline",
    out_dir: Path | None = None,
    target_skill_version: str | None = None,
) -> tuple[Path, Path]:
    """校验 evals → 拷成 <out>/<name>.json(标准格式,loader 零改动)→ 写 <name>.meta.json 溯源。

    返回 (baseline_json_path, meta_path)。同名已存在则覆盖(幂等)。
    evals.json 非法 → EvalLoader 抛(调用方报错)。
    """
    eval_set = EvalLoader().load(evals_path)
    out_dir = out_dir or evals_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    baseline_path = out_dir / f"{name}.json"
    meta_path = out_dir / f"{name}.meta.json"

    shutil.copy2(evals_path, baseline_path)
    meta = {
        "skill_name": eval_set.skill_name,
        "target_skill_version": target_skill_version,
        "skill_eval_version": skill_eval.__version__,
        "frozen_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_evals": evals_path.name,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return baseline_path, meta_path


def read_meta(evals_path: Path) -> dict | None:
    """读 evals_path 同名 .meta.json sidecar;无则 None(普通 evals.json)。

    baseline.json → baseline.meta.json(with_suffix 替换 .json 为 .meta.json)。
    """
    meta_path = evals_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def skill_name_mismatch(evals_path: Path, running_skill_name: str) -> str | None:
    """冻结基线的 skill_name ≠ 正在跑的 skill 名 → 返回警告串;否则 None。

    无 sidecar(普通 evals.json)→ None,不校验。只防"拿错 skill 的卷";
    同 skill 不同版本是预期用法(skill_name 相同)。
    """
    meta = read_meta(evals_path)
    if meta is None:
        return None
    baseline_name = meta.get("skill_name", "")
    if baseline_name and running_skill_name and baseline_name != running_skill_name:
        return (f"baseline skill_name '{baseline_name}' ≠ running skill '{running_skill_name}'"
                f" — possibly wrong exam paper")
    return None
