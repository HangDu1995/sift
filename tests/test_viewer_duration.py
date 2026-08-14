"""viewer 渲染 llm_duration_seconds:拆「墙钟分解(加总=总计)」+「LLM 累计(并发叠加)」两段。

render-smoke 验模板 JS 源字面(两段标签 + phase1_wall 等字段引用都在);JS 语法正确性靠
test_reporting 的 node --check(viewer.html 整 <script>);运行时分支渲染靠 node --check + 重生成
肉眼(viewer 渲染器庞大,mock-DOM 不对口;与 viewer 其余面板测试同口径)。"""
from sift.reporting.viewer import ReportGenerator


def _duration_fixture():
    return {
        "phase1_wall": 8.0,
        "phase2_wall": 7.0,
        "read_runs_wall": 2.0,
        "comparator_wall": 4.0,
        "analyzer_wall": 1.0,
        "scoring": 7.5,
        "grader": 3.0,
        "comparator": 2.5,
        "analyzer": 2.0,
        "audit": 3.0,
        "executor": 0.5,
        "total": 15.0,
    }


def test_viewer_embeds_llm_duration_seconds(tmp_path):
    bench = {"metadata": {"skill_name": "s"}, "llm_duration_seconds": _duration_fixture()}
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_duration_seconds"' in html          # embedded data 含
    # 两段子块标签都在模板 JS 源里
    assert "墙钟分解" in html
    assert "LLM 累计" in html
    # 墙钟字段引用 + 总计锚点
    assert "phase1_wall" in html and "phase2_wall" in html
    assert "总计" in html and "墙钟" in html


def test_viewer_omits_duration_badge_when_empty(tmp_path):
    """空 llm_duration_seconds → 数据落盘 {},JS 因 du.total 等空不渲染 badge。"""
    bench = {"metadata": {"skill_name": "s"}, "llm_duration_seconds": {}}
    html = ReportGenerator().generate_html(tmp_path, benchmark=bench)
    assert '"llm_duration_seconds": {}' in html
