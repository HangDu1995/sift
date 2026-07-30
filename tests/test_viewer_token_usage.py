"""Phase 2: viewer 渲染 llm_token_usage(分列 + 总计)。"""
from skill_eval.reporting.viewer import ReportGenerator


def test_viewer_embeds_llm_token_usage(tmp_path):
    bench = {
        "metadata": {"skill_name": "s"},
        "llm_token_usage": {
            "scoring": {"input_tokens": 30, "output_tokens": 10, "total_cost_usd": 0.01, "calls": 1},
            "audit": {"input_tokens": 40, "output_tokens": 5, "total_cost_usd": 0.02, "calls": 1},
            "executor": {"input_tokens": 100, "output_tokens": 0},
            "total": {"input_tokens": 170, "output_tokens": 15, "total_cost_usd": 0.03},
        },
    }
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_token_usage"' in html
    assert '"scoring"' in html and '"audit"' in html and '"total"' in html
    assert "_tokSeg" in html   # 模板含渲染逻辑(JS 在浏览器运行;渲染输出由 node -e 验)


def test_viewer_omits_badge_when_empty(tmp_path):
    """空 llm_token_usage → 数据落盘 {},JS 因 tu.total 空不渲染 badge。"""
    bench = {"metadata": {"skill_name": "s"}, "llm_token_usage": {}}
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_token_usage": {}' in html
