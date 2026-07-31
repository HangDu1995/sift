"""Phase 2.5: viewer 渲染 llm_duration_seconds(分列 + 墙钟 total,不对账分列)。"""
from skill_eval.reporting.viewer import ReportGenerator


def test_viewer_embeds_llm_duration_seconds(tmp_path):
    bench = {
        "metadata": {"skill_name": "s"},
        "llm_duration_seconds": {
            "scoring": 7.5,
            "audit": 3.0,
            "executor": 0.5,
            "total": 15.0,
        },
    }
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_duration_seconds"' in html          # embedded data 含
    assert "总计耗时" in html and "墙钟" in html       # 模板 badge 行
    assert "_dur" in html                              # 渲染逻辑(JS 在浏览器跑;变换由 node -e 验)


def test_viewer_omits_duration_badge_when_empty(tmp_path):
    """空 llm_duration_seconds → 数据落盘 {},JS 因 du.total 等空不渲染 badge。"""
    bench = {"metadata": {"skill_name": "s"}, "llm_duration_seconds": {}}
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_duration_seconds": {}' in html
