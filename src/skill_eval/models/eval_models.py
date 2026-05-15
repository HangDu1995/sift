"""Eval definition models."""

from __future__ import annotations

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

    def get_name(self) -> str:
        return self.name or f"eval-{self.id}"

    def setup_fixtures(self, base_dir: Path) -> None:
        for f in self.fixtures:
            target = base_dir / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")


class EvalSet(BaseModel):
    skill_name: str
    evals: list[EvalCase]
