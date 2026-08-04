"""critique 报告模型(SKILL.md 正文内容质量诊断,4 维读者反馈态)。

status 三态:met 满足 / partial 部分 / missing 缺失(不用 n/a——4 条 rubric 对任何
skill 都适用,极简 skill 没"高风险操作"→ missing 正好提醒补)。
"""
from pydantic import BaseModel, Field


class CritiqueDimension(BaseModel):
    key: str          # failure_mechanism / actionable_specificity / high_risk_blacklist / examples
    name: str         # 中文显示名
    status: str = ""  # met / partial / missing(空=未填,由 _parse 填)
    # 每处引用一条(LLM 自报,mostly-verbatim 非保证逐字);分属不同段落就拆多条,missing 空 []
    evidence: list[str] = Field(default_factory=list)
    feedback: str = ""   # 认可/已做到的大白话(met 好在哪;partial 已做到的;missing 空)
    gap: str = ""        # 缺口+方向的大白话(met 空;partial/missing 缺什么+怎么补)
    vote_tally: str = ""  # 多数投票可信度"X/N"(仅 --runs>1 有;单次跑空)


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
