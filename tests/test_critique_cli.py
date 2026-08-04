"""critique CLI 单测(CliRunner)。"""
import json
from typer.testing import CliRunner

from skill_eval.cli.commands import critique as crt_cmd
from skill_eval.models.critique_report import ContentQualityReport, CritiqueDimension


def _fake_report():
    return ContentQualityReport(
        skill_name="sk",
        dimensions=[
            CritiqueDimension(key="failure_mechanism", name="失败机制编码", status="met",
                              evidence=["漏 DB_HOST 会超时", "分页上限静默丢数据"], feedback="写清了失败机制", gap=""),
            CritiqueDimension(key="actionable_specificity", name="可执行具体性", status="partial",
                              evidence=["部署前检查"], feedback="有步骤", gap="可补具体命令"),
            CritiqueDimension(key="high_risk_blacklist", name="高风险黑名单", status="missing",
                              evidence=[], feedback="", gap="补:别用 --force"),
            CritiqueDimension(key="examples", name="示例驱动", status="missing",
                              evidence=[], feedback="", gap="补 worked example"),
        ],
        token_usage={"input_tokens": 100, "output_tokens": 50}, duration_seconds=2.5,
    )


def _make_skill(tmp_path):
    skill = tmp_path / "sk"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: sk\ndescription: d\n---\n# S\n规则X\n", encoding="utf-8")
    return skill


def test_critique_reports_dims_and_writes_json(monkeypatch, tmp_path):
    runner = CliRunner()
    skill = _make_skill(tmp_path)
    monkeypatch.setattr(crt_cmd, "analyze_content_quality", lambda *a, **kw: _fake_report())
    out = tmp_path / "out"
    r = runner.invoke(crt_cmd.app, ["critique", str(skill), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert "满足" in r.output and "部分" in r.output and "缺失" in r.output  # 三态汇总
    assert "失败机制编码" in r.output                   # 维度名
    assert "漏 DB_HOST" in r.output                    # evidence 上报
    assert "✓" in r.output and "✗" in r.output          # 认可(✓)与缺口(✗)分行
    assert "可补具体命令" in r.output                   # partial 的 gap 内容上报
    assert (out / "content-quality-report.json").exists()
    js = json.loads((out / "content-quality-report.json").read_text(encoding="utf-8"))
    assert len(js["dimensions"]) == 4


def test_critique_does_not_require_evals_json(monkeypatch, tmp_path):
    """critique 不依赖 evals.json(纯读 body)——无 evals.json 也照跑。"""
    runner = CliRunner()
    skill = _make_skill(tmp_path)
    assert not (skill / "evals" / "evals.json").exists()  # 确实没 evals.json
    monkeypatch.setattr(crt_cmd, "analyze_content_quality", lambda *a, **kw: _fake_report())
    r = runner.invoke(crt_cmd.app, ["critique", str(skill), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output                   # 不报缺 evals.json
