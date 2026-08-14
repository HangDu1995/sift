"""Phase 2: viewer 渲染 llm_token_usage(分列 + 总计)。"""
from sift.reporting.viewer import ReportGenerator


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


def test_viewer_embeds_per_eval_tokens(tmp_path):
    """per-eval token 列:benchmark.runs[].result.tokens 嵌入 + 模板含渲染逻辑。"""
    bench = {
        "metadata": {"skill_name": "s"},
        "runs": [
            {"eval_id": 1, "configuration": "with_skill", "run_number": 1,
             "result": {"tokens": 12345, "time_seconds": 12.0, "pass_rate": 1.0}},
            {"eval_id": 2, "configuration": "with_skill", "run_number": 1,
             "result": {"tokens": 67890, "time_seconds": 34.0, "pass_rate": 0.5}},
        ],
    }
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert "12345" in html and "67890" in html          # per-eval token 嵌入
    assert "hasTokens" in html and "tokDisplay" in html  # 模板含 token 列逻辑


def test_viewer_embeds_scoring_breakdown(tmp_path):
    """scoring 细分:llm_token_usage 含 grader/comparator/analyzer 段 + 模板含三行逻辑。"""
    bench = {
        "metadata": {"skill_name": "s"},
        "llm_token_usage": {
            "scoring": {"input_tokens": 60, "output_tokens": 20, "calls": 3},
            "grader": {"input_tokens": 30, "output_tokens": 10, "calls": 1},
            "comparator": {"input_tokens": 20, "output_tokens": 5, "calls": 1},
            "analyzer": {"input_tokens": 10, "output_tokens": 5, "calls": 1},
            "audit": {"input_tokens": 40, "output_tokens": 5},
            "executor": {"input_tokens": 100, "output_tokens": 0},
            "total": {"input_tokens": 200, "output_tokens": 25},
        },
        "llm_duration_seconds": {
            "scoring": 30.0, "grader": 10.0, "comparator": 15.0, "analyzer": 5.0,
            "audit": 20.0, "executor": 100.0, "total": 150.0,
        },
    }
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"grader"' in html and '"comparator"' in html and '"analyzer"' in html
    assert "var gr = tu.grader" in html                  # 模板含三行渲染逻辑
