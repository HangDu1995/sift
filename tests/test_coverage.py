"""coverage grid 单测(analyzer + CLI)。"""
from skill_eval.models.coverage_report import CoveragePoint, CoverageReport


def test_coverage_point_defaults():
    p = CoveragePoint(label="安全事件升级")
    assert p.label == "安全事件升级"
    assert p.source_quote == ""
    assert p.covered_by == []


def test_report_gaps_are_zero_coverage_points():
    """gaps = points 里 covered_by 为空的(自动派生,不是独立字段)。"""
    rep = CoverageReport(
        skill_name="my-skill",
        points=[
            CoveragePoint(label="已覆盖点", source_quote="q1", covered_by=[1, 3]),
            CoveragePoint(label="漏测点", source_quote="q2", covered_by=[]),
        ],
    )
    assert len(rep.gaps) == 1
    assert rep.gaps[0].label == "漏测点"


def test_report_roundtrip():
    rep = CoverageReport(
        skill_name="my-skill",
        points=[CoveragePoint(label="p", source_quote="q", covered_by=[2])],
        token_usage={"total_tokens": 500},
        duration_seconds=3.2,
    )
    js = rep.model_dump_json()
    assert '"gaps"' not in js  # gaps 是派生属性,不进 JSON(防止将来误序列化)
    rep2 = CoverageReport.model_validate_json(js)
    assert rep2.points[0].covered_by == [2]
    assert rep2.gaps == []
