"""Analysis models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AnalysisImprovementSuggestion(BaseModel):
    priority: str = "medium"
    category: str = ""
    suggestion: str = ""
    expected_impact: str = ""


class AnalysisInstructionFollowing(BaseModel):
    score: int | None = None
    issues: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    winner_strengths: list[str] = Field(default_factory=list)
    loser_weaknesses: list[str] = Field(default_factory=list)
    instruction_following: dict[str, AnalysisInstructionFollowing] = Field(default_factory=dict)
    improvement_suggestions: list[AnalysisImprovementSuggestion] = Field(default_factory=list)
    transcript_insights: dict[str, str] = Field(default_factory=dict)
