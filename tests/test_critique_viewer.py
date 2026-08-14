"""critique HTML 报告单测(生成 + JS 校验)。"""
import json
import re
import subprocess
from pathlib import Path

from sift.models.critique_report import ContentQualityReport, CritiqueDimension
from sift.reporting.critique_viewer import CritiqueReportGenerator


def _sample_report():
    return ContentQualityReport(
        skill_name="sk",
        dimensions=[
            CritiqueDimension(key="failure_mechanism", name="失败机制编码", status="met",
                              evidence=["漏 DB_HOST 超时", "分页上限静默丢数据"], feedback="有因有果,挺好", gap=""),
            CritiqueDimension(key="examples", name="示例驱动", status="missing", feedback="", gap="补真实例子", vote_tally="3/3"),
            CritiqueDimension(key="desc_purpose_clarity", name="意图清晰", group="description",
                              status="met", feedback="清楚", gap=""),
            CritiqueDimension(key="desc_trigger_specificity", name="触发具体性", group="description",
                              status="partial", feedback="有限定", gap="补排除条件"),
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
    assert len(data["dimensions"]) == 4


def test_template_script_passes_node_check():
    """抽 <script> 段跑 node --check 验语法(项目硬规矩)。"""
    tpl = (Path(__file__).resolve().parents[1] / "src" / "sift" / "reporting"
           / "templates" / "critique-report.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", tpl, re.S)
    assert m, "模板缺 <script> 段"
    js = m.group(1).replace("/*__EMBEDDED_DATA__*/;", "var EMBEDDED_DATA = {};")
    r = subprocess.run(["node", "--check"], input=js, text=True, capture_output=True)
    assert r.returncode == 0, f"JS 语法错:\n{r.stderr}"


def test_render_real_data_has_dims_and_badges():
    """抽 <script> 注入真实数据跑 node,实际执行 JS 字符串变换——验证渲染输出含
    维度名 / status badge / 每处 evidence 分行 / 计数标签(读取路径,非静态模板串)。"""
    tpl = (Path(__file__).resolve().parents[1] / "src" / "sift" / "reporting"
           / "templates" / "critique-report.html").read_text(encoding="utf-8")
    m = re.search(r"<script>(.*?)</script>", tpl, re.S)
    js = m.group(1)
    data = _sample_report().model_dump()
    js = js.replace("/*__EMBEDDED_DATA__*/;",
                    "var EMBEDDED_DATA = " + json.dumps(data, ensure_ascii=False) + ";")
    js = js.replace("document.getElementById('critique-root').innerHTML = html;",
                    "console.log(html);")
    r = subprocess.run(["node"], input=js, text=True, capture_output=True)
    assert r.returncode == 0, f"JS 执行错:\n{r.stderr}"
    out = r.stdout
    assert "失败机制编码" in out                              # 维度名
    assert "引用原文(2 处)" in out                            # 多 evidence 计数标签
    assert "漏 DB_HOST 超时" in out and "分页上限静默丢数据" in out  # 每处分行渲染
    assert "缺失" in out                                       # status badge
    assert "✓" in out and "✗" in out                          # 认可(✓)/缺口(✗)分行
    assert "有因有果" in out and "补真实例子" in out            # feedback + gap 内容
    assert "(3/3)" in out                                       # vote_tally 可信度
    # group 分节(正文 / description)——数据形状相关分支,render-smoke 抓不到,这里实跑 node 验
    assert "正文内容质量" in out and "description 质量" in out  # 两个分节标题都渲染
    assert "意图清晰" in out and "触发具体性" in out            # desc 维名
