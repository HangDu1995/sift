"""Eval definition models."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from skill_eval.models.enums import AssertionTier, AssertionType


class Assertion(BaseModel):
    name: str
    type: AssertionType
    params: dict[str, Any] = Field(default_factory=dict)
    tier: AssertionTier = AssertionTier.TIER1
    retry_on_uneval: bool = True
    retry_max: int = 2


class FixtureFile(BaseModel):
    path: str
    content: str = ""


class EvalCase(BaseModel):
    id: int
    name: str = ""
    prompt: str
    expected_output: str = ""
    files: list[str] = Field(default_factory=list)
    expectations: list[str] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    fixtures: list[FixtureFile] = Field(default_factory=list)
    level: str = "L1"
    mode: str = ""
    source_dir: str = ""
    source_dir_as: str = ""
    timeout: int | None = None  # per-eval 覆盖全局 timeout(P1-3a);None 走 RunConfig.timeout_seconds
    flags: list[str] = Field(default_factory=list)   # 后处理标记(如 degraded/review),默认空,向后兼容

    def get_name(self) -> str:
        return self.name or f"eval-{self.id}"

    def setup_fixtures(self, base_dir: Path) -> None:
        for f in self.fixtures:
            target = base_dir / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")

    def setup_source(self, skill_dir: Path, work_dir: Path) -> tuple[str, str]:
        if not self.source_dir:
            return "", ""
        src = skill_dir / self.source_dir if not Path(self.source_dir).is_absolute() else Path(self.source_dir)
        if not src.exists():
            return "", ""
        dst_name = self.source_dir_as or src.name
        dst = work_dir / dst_name
        if not dst.exists():
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        return str(dst), src.name


class EvalSet(BaseModel):
    skill_name: str
    evals: list[EvalCase]
