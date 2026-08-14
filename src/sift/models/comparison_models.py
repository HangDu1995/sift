"""Comparison models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComparisonRubricScores(BaseModel):
    correctness: int = 0
    completeness: int = 0
    accuracy: int = 0


class ComparisonStructureScores(BaseModel):
    organization: int = 0
    formatting: int = 0
    usability: int = 0


class ComparisonOutputRubric(BaseModel):
    content: ComparisonRubricScores = Field(default_factory=ComparisonRubricScores)
    structure: ComparisonStructureScores = Field(default_factory=ComparisonStructureScores)
    content_score: float = 0.0
    structure_score: float = 0.0
    overall_score: float = 0.0


class ComparisonOutputQuality(BaseModel):
    score: int = 0
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class ComparisonExpectationDetail(BaseModel):
    text: str = ""
    passed: bool = False


class ComparisonExpectationResults(BaseModel):
    passed: int = 0
    total: int = 0
    pass_rate: float = 0.0
    details: list[ComparisonExpectationDetail] = Field(default_factory=list)


class ComparisonResult(BaseModel):
    winner: str = ""
    reasoning: str = ""
    rubric: dict[str, ComparisonOutputRubric] = Field(default_factory=dict)
    output_quality: dict[str, ComparisonOutputQuality] = Field(default_factory=dict)
    expectation_results: dict[str, ComparisonExpectationResults] | None = None
    # failed=True 表示这次比较本身失败(调用失败/解析失败),winner 降级 tie。
    # 供 _compare_with_retry 识别并重试(否则失败被吞成静默 tie,重试循环成死代码)。
    failed: bool = False
    # 揭盲位置映射:comparator 随机打乱哪侧放 LLM position A/B,_translate 后记下 A/B 各自
    # 对应的语义 label(如 pos_a_label="with")。viewer 在 reasoning 旁标注,否则 reasoning
    # 里的 A/B 与去匿名化的 winner(如 "with")对不上。未过 _translate(直接构造/解析失败)→ 空。
    pos_a_label: str = ""
    pos_b_label: str = ""
