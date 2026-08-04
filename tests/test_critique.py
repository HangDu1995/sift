"""critique 单测(模型 + prompt + parse + analyze)。"""
from skill_eval.models.critique_report import ContentQualityReport, CritiqueDimension


def test_dimension_defaults():
    d = CritiqueDimension(key="examples", name="示例驱动")
    assert d.key == "examples"
    assert d.status == ""          # 默认空(由 _parse 填)
    assert d.evidence == ""
    assert d.feedback == ""


def test_report_status_counts_derived():
    """status_counts 从 dimensions 派生(met/partial/missing 计数),不进 JSON。"""
    rep = ContentQualityReport(
        skill_name="sk",
        dimensions=[
            CritiqueDimension(key="failure_mechanism", name="失败机制编码", status="met"),
            CritiqueDimension(key="actionable_specificity", name="可执行具体性", status="partial"),
            CritiqueDimension(key="high_risk_blacklist", name="高风险黑名单", status="missing"),
            CritiqueDimension(key="examples", name="示例驱动", status="met"),
            CritiqueDimension(key="other", name="非法", status="bogus"),  # 非法 status 不计入
        ],
    )
    assert rep.status_counts == {"met": 2, "partial": 1, "missing": 1}


def test_report_roundtrip_no_counts_in_json():
    rep = ContentQualityReport(
        skill_name="sk",
        dimensions=[CritiqueDimension(key="examples", name="示例驱动", status="met")],
        token_usage={"input_tokens": 10}, duration_seconds=1.5,
    )
    js = rep.model_dump_json()
    assert "status_counts" not in js           # 派生属性不序列化
    rep2 = ContentQualityReport.model_validate_json(js)
    assert rep2.dimensions[0].status == "met"
    assert rep2.status_counts == {"met": 1, "partial": 0, "missing": 0}
