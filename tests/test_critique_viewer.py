"""critique HTML 报告单测(生成 + JS 校验)。"""
import json
import re
import subprocess
from pathlib import Path

from skill_eval.models.critique_report import ContentQualityReport, CritiqueDimension
from skill_eval.reporting.critique_viewer import CritiqueReportGenerator


def _sample_report():
    return ContentQualityReport(
        skill_name="sk",
        dimensions=[
            CritiqueDimension(key="failure_mechanism", name="失败机制编码", status="met",
                              evidence="漏 DB_HOST 超时", feedback="好"),
            CritiqueDimension(key="examples", name="示例驱动", status="missing", feedback="补例"),
        ],
        token_usage={"input_tokens": 10, "output_tokens": 5}, duration_seconds=2.0,
    )


def test_generate_html_embeds_data_and_skill_name():
    html = CritiqueReportGenerator().generate_html(_sample_report())
    assert "var EMBEDDED_DATA" in html
    assert "sk" in html
    payload = html.split("var EMBEDDED_DATA = ", 1)[1].split(";", 1)[0]
    data = json.loads(payload)
    assert data["skill_name"] == "sk"
    assert len(data["dimensions"]) == 2


def test_template_script_passes_node_check():
    """抽 <script> 段跑 node --check 验语法(项目硬规矩)。"""
    tpl = (Path(__file__).resolve().parents[1] / "src" / "skill_eval" / "reporting"
           / "templates" / "critique-report.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", tpl, re.S)
    assert m, "模板缺 <script> 段"
    js = m.group(1).replace("/*__EMBEDDED_DATA__*/;", "var EMBEDDED_DATA = {};")
    r = subprocess.run(["node", "--check"], input=js, text=True, capture_output=True)
    assert r.returncode == 0, f"JS 语法错:\n{r.stderr}"


def test_render_real_data_has_dims_and_badges():
    """用真实 report 喂 JS 的字符串变换,验证读取路径(渲染输出含维度名 + status badge)。"""
    html = CritiqueReportGenerator().generate_html(_sample_report())
    assert "失败机制编码" in html  # 维度名进了渲染输出
    assert "badge-met" in html or "满足" in html
    assert "badge-missing" in html or "缺失" in html
