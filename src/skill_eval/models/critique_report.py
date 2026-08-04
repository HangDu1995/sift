"""critique 报告模型(SKILL.md 正文内容质量诊断,4 维读者反馈态)。

status 三态:met 满足 / partial 部分 / missing 缺失(不用 n/a——4 条 rubric 对任何
skill 都适用,极简 skill 没"高风险操作"→ missing 正好提醒补)。
"""
from pydantic import BaseModel, Field


class CritiqueDimension(BaseModel):
    key: str          # failure_mechanism / actionable_specificity / high_risk_blacklist / examples
    name: str         # 中文显示名
    status: str = ""  # met / partial / missing(空=未填,由 _parse 填)
    evidence: str = ""   # 引用的 SKILL.md 正文片段(LLM 自报,mostly-verbatim;missing 可空)
    feedback: str = ""   # 读者反馈(met 写好在哪,partial/missing 写怎么改)


class ContentQualityReport(BaseModel):
    skill_name: str
    dimensions: list[CritiqueDimension] = Field(default_factory=list)
    token_usage: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def status_counts(self) -> dict[str, int]:
        """各 status 计数(派生,终端/HTML 汇总用;不进 JSON)。"""
        counts = {"met": 0, "partial": 0, "missing": 0}
        for d in self.dimensions:
            if d.status in counts:
                counts[d.status] += 1
        return counts
