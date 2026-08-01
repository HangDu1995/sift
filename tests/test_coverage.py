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


from pathlib import Path
from skill_eval.evals.coverage import analyze_coverage, _parse_coverage_response
from skill_eval.models import EvalSet, EvalCase


def _fake_response_two_points():
    return '{"points": [{"label": "分类错误码", "source_quote": "映射到已知码", "covered_by": [1, 6]}, {"label": "安全事件升级", "source_quote": "若日志提到安全事件", "covered_by": []}]}'


def test_parse_response_builds_points_and_gaps():
    rep = _parse_coverage_response(_fake_response_two_points(), "my-skill")
    assert len(rep.points) == 2
    assert rep.points[0].covered_by == [1, 6]
    assert [g.label for g in rep.gaps] == ["安全事件升级"]


def test_parse_response_malformed_returns_empty():
    rep = _parse_coverage_response("not json {", "my-skill")
    assert rep.points == [] and rep.gaps == []


def test_analyze_coverage_calls_claude_and_maps(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")
    captured = {}

    def fake_call(prompt, model=None, timeout=120, max_turns=None, on_usage=None):
        captured["prompt"] = prompt
        if on_usage:
            on_usage({"input_tokens": 100, "output_tokens": 50})
        return _fake_response_two_points()

    import skill_eval.evals.coverage as cov
    monkeypatch.setattr(cov, "call_claude", fake_call)

    eval_set = EvalSet(skill_name="sk", evals=[
        EvalCase(id=1, prompt="分析日志", rationale="映射到已知码"),
        EvalCase(id=6, prompt="多错误", rationale="line_number"),
    ])
    rep = cov.analyze_coverage(skill, eval_set, model=None, timeout=120)

    assert "规则X" in captured["prompt"]            # SKILL.md 正文喂进去了
    assert "分析日志" in captured["prompt"]          # eval prompt+rationale 喂进去了
    assert len(rep.points) == 2
    assert rep.token_usage.get("input_tokens") == 100   # on_usage 收了 token
    assert rep.duration_seconds >= 0.0


def test_parse_response_strips_json_fence():
    fenced = '```json\n{"points": [{"label": "x", "source_quote": "q", "covered_by": ["2"]}]}\n```'
    rep = _parse_coverage_response(fenced, "my-skill")
    assert len(rep.points) == 1
    assert rep.points[0].covered_by == [2]   # string id "2" coerced + fence stripped


def test_analyze_coverage_call_failure_returns_empty(monkeypatch, tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")

    import skill_eval.evals.coverage as cov
    monkeypatch.setattr(cov, "call_claude", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    eval_set = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="分析日志", rationale="r")])
    rep = cov.analyze_coverage(skill, eval_set, model=None, timeout=120)

    assert rep.points == []
    assert rep.duration_seconds >= 0.0
