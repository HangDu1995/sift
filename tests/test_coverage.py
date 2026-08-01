"""coverage grid 单测(analyzer + CLI)。"""
import json
from pathlib import Path

from typer.testing import CliRunner

import skill_eval.evals.coverage as cov
from skill_eval.cli.commands import coverage as cov_cmd
from skill_eval.evals.coverage import analyze_coverage, _parse_coverage_response
from skill_eval.models import EvalSet, EvalCase
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

    monkeypatch.setattr(cov, "call_claude", fake_call)

    eval_set = EvalSet(skill_name="sk", evals=[
        EvalCase(id=1, prompt="分析日志", rationale="映射到已知码"),
        EvalCase(id=6, prompt="多错误", rationale="line_number"),
    ])
    rep = analyze_coverage(skill, eval_set, model=None, timeout=120)

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

    monkeypatch.setattr(cov, "call_claude", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    eval_set = EvalSet(skill_name="sk", evals=[EvalCase(id=1, prompt="分析日志", rationale="r")])
    rep = analyze_coverage(skill, eval_set, model=None, timeout=120)

    assert rep.points == []
    assert rep.duration_seconds >= 0.0


def test_coverage_default_reports_gaps_and_writes_json(monkeypatch, tmp_path):
    runner = CliRunner()

    skill = tmp_path / "sk"
    edir = skill / "evals"
    edir.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")
    (edir / "evals.json").write_text(json.dumps({
        "skill_name": "sk",
        "evals": [{"id": 1, "prompt": "分析日志", "rationale": "映射到已知码"}],
    }, ensure_ascii=False), encoding="utf-8")

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120):
        return CoverageReport(skill_name="sk", points=[
            CoveragePoint(label="已覆盖", source_quote="q1", covered_by=[1]),
            CoveragePoint(label="安全事件升级", source_quote="若日志提到安全事件", covered_by=[]),
        ], token_usage={"input_tokens": 100, "output_tokens": 50}, duration_seconds=2.5)

    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    out = tmp_path / "out"
    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "[cyan]" not in r.output               # 富文本标记不能漏到纯文本输出(markup=False)
    assert "安全事件升级" in r.output                  # 漏测点上报
    assert "init" in r.output and "--focus" in r.output  # 给了补题处方
    assert (out / "coverage-report.json").exists()
    js = json.loads((out / "coverage-report.json").read_text(encoding="utf-8"))
    assert any(p["covered_by"] == [] for p in js["points"])


def test_coverage_no_gaps_reports_all_covered(monkeypatch, tmp_path):
    runner = CliRunner()
    skill = tmp_path / "sk"; edir = skill / "evals"; edir.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")
    (edir / "evals.json").write_text(json.dumps({"skill_name": "sk", "evals": [{"id": 1, "prompt": "p"}]}), encoding="utf-8")

    def fake_analyze(skill_path, eval_set, *, model=None, timeout=120):
        return CoverageReport(skill_name="sk", points=[
            CoveragePoint(label="全覆盖", source_quote="q", covered_by=[1])])
    monkeypatch.setattr(cov_cmd, "analyze_coverage", fake_analyze)

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0
    assert "无漏测" in r.output or "全覆盖" in r.output


def test_coverage_missing_evals_json_warns(tmp_path):
    """无 evals.json(用户没跑 init)→ 黄字提示 + exit 0,不抛 traceback。"""
    runner = CliRunner()
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")

    r = runner.invoke(cov_cmd.app, ["coverage", str(skill)])
    assert r.exit_code == 0
    assert "不存在" in r.output or "先跑 init" in r.output
