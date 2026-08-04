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


from skill_eval.static_analysis.critique import _build_critique_prompt, _parse_critique_response


def test_build_prompt_contains_four_rubrics_and_body():
    prompt = _build_critique_prompt("my-skill", "## 部署\n执行 deploy.sh\n")
    assert "my-skill" in prompt
    assert "执行 deploy.sh" in prompt            # body 喂进去了
    for key in ("failure_mechanism", "actionable_specificity", "high_risk_blacklist", "examples"):
        assert key in prompt                     # 4 条 rubric key 都在
    assert "好例" in prompt and "反例" in prompt  # 每条带好例/反例
    assert "JSON ONLY" in prompt


def test_parse_response_normal_four_dims():
    resp = '{"dimensions": [{"key": "failure_mechanism", "status": "met", "evidence": "e1", "feedback": "f1"}, {"key": "actionable_specificity", "status": "partial", "evidence": "e2", "feedback": "f2"}, {"key": "high_risk_blacklist", "status": "missing", "evidence": "", "feedback": "f3"}, {"key": "examples", "status": "missing", "evidence": "", "feedback": "f4"}]}'
    rep = _parse_critique_response(resp, "sk")
    assert [d.key for d in rep.dimensions] == ["failure_mechanism", "actionable_specificity", "high_risk_blacklist", "examples"]
    assert rep.dimensions[0].status == "met"
    assert rep.dimensions[0].name == "失败机制编码"   # 中文名按 key 归位
    assert rep.status_counts == {"met": 1, "partial": 1, "missing": 2}


def test_parse_response_reorders_by_key():
    """LLM 乱序返回 → 按 _RUBRIC 固定顺序归位。"""
    resp = '{"dimensions": [{"key": "examples", "status": "met"}, {"key": "failure_mechanism", "status": "partial"}]}'
    rep = _parse_critique_response(resp, "sk")
    assert rep.dimensions[0].key == "failure_mechanism"  # 归位到首位
    assert rep.dimensions[3].key == "examples"


def test_parse_response_missing_dim_defaults_missing():
    """LLM 漏掉某条 → 该条补默认 missing + feedback 提示。"""
    resp = '{"dimensions": [{"key": "failure_mechanism", "status": "met"}]}'  # 只返回 1 条
    rep = _parse_critique_response(resp, "sk")
    assert len(rep.dimensions) == 4              # 仍 4 条
    examples = [d for d in rep.dimensions if d.key == "examples"][0]
    assert examples.status == "missing"
    assert "未评审" in examples.feedback


def test_parse_response_invalid_status_defaults_missing():
    resp = '{"dimensions": [{"key": "failure_mechanism", "status": "great", "feedback": "x"}]}'
    rep = _parse_critique_response(resp, "sk")
    assert rep.dimensions[0].status == "missing"


def test_parse_response_malformed_returns_empty():
    rep = _parse_critique_response("not json {", "sk")
    assert rep.dimensions == []                  # 畸形 → 空 report(宁少报不乱报)


def test_parse_response_strips_json_fence():
    fenced = '```json\n{"dimensions": [{"key": "failure_mechanism", "status": "met"}]}\n```'
    rep = _parse_critique_response(fenced, "sk")
    assert len(rep.dimensions) == 4              # fence 剥离 + 漏条补齐
    assert rep.dimensions[0].status == "met"


import skill_eval.static_analysis.critique as crt
from skill_eval.static_analysis.critique import analyze_content_quality


def _fake_response_four_dims():
    return '{"dimensions": [{"key": "failure_mechanism", "status": "met", "evidence": "e1", "feedback": "好"}, {"key": "actionable_specificity", "status": "partial", "evidence": "e2", "feedback": "改"}, {"key": "high_risk_blacklist", "status": "missing", "evidence": "", "feedback": "补"}, {"key": "examples", "status": "missing", "evidence": "", "feedback": "补例"}]}'


def test_analyze_calls_claude_and_fills_body(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n执行 deploy.sh\n", encoding="utf-8")
    captured = {}

    def fake_call(prompt, model=None, timeout=120, max_turns=None, on_usage=None):
        captured["prompt"] = prompt
        if on_usage:
            on_usage({"input_tokens": 100, "output_tokens": 50})
        return _fake_response_four_dims()

    monkeypatch.setattr(crt, "call_claude", fake_call)
    rep = analyze_content_quality(skill, model=None, timeout=120)

    assert "执行 deploy.sh" in captured["prompt"]     # body_text 喂进去了
    assert "description" not in captured["prompt"].split("SKILL.md 正文")[1].split("##")[0]  # 不喂 frontmatter
    assert len(rep.dimensions) == 4
    assert rep.token_usage.get("input_tokens") == 100  # on_usage 收了 token
    assert rep.duration_seconds >= 0.0


def test_analyze_call_failure_returns_empty(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")
    monkeypatch.setattr(crt, "call_claude", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    rep = analyze_content_quality(skill, model=None, timeout=120)
    assert rep.dimensions == []                        # 失败 → 空 report(诊断不炸)
    assert rep.duration_seconds >= 0.0
