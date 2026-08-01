"""coverage grid 报告模型(eval 对 SKILL.md 内容点的覆盖)。"""
from pydantic import BaseModel, Field


class CoveragePoint(BaseModel):
    label: str                           # 内容点短名(SKILL.md 一条可测指令)
    source_quote: str = ""               # 该点对应的 SKILL.md 原文片段(LLM 自报,mostly-verbatim 非保证逐字)
    covered_by: list[int] = Field(default_factory=list)  # 覆盖该点的 eval id;空 = 漏测


class CoverageReport(BaseModel):
    skill_name: str
    points: list[CoveragePoint] = Field(default_factory=list)
    token_usage: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def gaps(self) -> list[CoveragePoint]:
        """漏测点 = 0 覆盖的内容点。派生属性,不存 JSON(避免与 points 失同步)。"""
        return [p for p in self.points if not p.covered_by]
