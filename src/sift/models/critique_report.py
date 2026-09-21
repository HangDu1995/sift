"""critique 报告模型(声明内容质量诊断:正文 4 维 + description 2 维,读者反馈态)。

status 三态:met 满足 / partial 部分 / missing 缺失(不用 n/a——rubric 对任何声明都适用,
极简 skill 没"高风险操作"→ missing 正好提醒补)。每维带 ``group``(正文 / description),
报告里分节显示、JSON 自带分组归属。
"""

from pydantic import BaseModel, Field


class CritiqueDimension(BaseModel):
    # failure_mechanism / actionable_specificity / high_risk_blacklist / examples / desc_purpose_clarity /
    # desc_trigger_specificity
    key: str
    name: str  # 中文显示名
    group: str = "正文"  # 正文 / description(这维评的是 body 还是 frontmatter description)
    status: str = ""  # met / partial / missing(空=未填,由 _parse 填)
    # 每处引用一条(LLM 自报,mostly-verbatim 非保证逐字);分属不同段落就拆多条,missing 空 []
    evidence: list[str] = Field(default_factory=list)
    feedback: str = ""  # 认可/已做到的大白话(met 好在哪;partial 已做到的;missing 空)
    gap: str = ""  # 缺口+方向的大白话(met 空;partial/missing 缺什么+怎么补)
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
            if not (d.status in counts):
                continue
            counts[d.status] += 1
        return counts
