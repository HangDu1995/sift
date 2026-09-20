"""viewer 渲染:win-rate Wilson CI + 显著性(读 comparison_summary)+ flaky 红旗。

运行时 JS 正确性由 node -e 验(flakyOf / sig 变换);此处验模板嵌入字段 + 含逻辑字面量。
"""

from sift.reporting.viewer import ReportGenerator


def test_viewer_embeds_win_rate_ci(tmp_path):
    """comparison_summary 的 win_rate_ci_lo/hi 嵌入 + 模板含显著性渲染逻辑。"""
    bench = {
        "metadata": {"skill_name": "s"},
        "comparison_summary": {
            "total": 10,
            "a_wins": 9,
            "b_wins": 0,
            "ties": 1,
            "failed": 0,
            "valid": 10,
            "win_rate": 0.9,
            "win_rate_ci_lo": 0.6,
            "win_rate_ci_hi": 0.99,
            "label_a": "with",
            "label_b": "without",
        },
    }
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert "win_rate_ci_lo" in html and "win_rate_ci_hi" in html  # 字段嵌入 + JS 读取
    assert "显著更优" in html  # 模板含显著性判定字面量
    assert "不显著" in html  # 模板含不显著分支


def test_viewer_template_has_flaky_logic(tmp_path):
    """模板含 flakyOf 函数 + 补跑建议 tooltip(运行时正确性由 node -e 验)。"""
    html = ReportGenerator().generate_html(tmp_path, benchmark={"metadata": {"skill_name": "s"}})
    assert "flakyOf" in html  # flaky 计算函数
    assert "建议补跑" in html  # 红旗 tooltip 的补跑命令
    assert "pass_rate_programmatic" in html  # flaky 取值字段
