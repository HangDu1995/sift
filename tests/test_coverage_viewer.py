"""coverage HTML 报告单测(镜像 test_trigger_viewer)。"""
import re
import subprocess
import tempfile

from sift.models.coverage_report import CoveragePoint, CoverageReport
from sift.reporting.coverage_viewer import CoverageReportGenerator


def _sample_report():
    return CoverageReport(
        skill_name="my-skill",
        points=[
            CoveragePoint(label="已覆盖", source_quote="q1", covered_by=[1, 3]),
            CoveragePoint(label="安全事件升级", source_quote="若日志提到安全事件", covered_by=[]),
        ],
        token_usage={"input_tokens": 100, "output_tokens": 50},
        duration_seconds=2.5,
    )


def test_html_replaces_placeholder_and_has_root():
    html = CoverageReportGenerator().generate_html(_sample_report())
    assert "/*__EMBEDDED_DATA__*/" not in html
    assert 'id="coverage-root"' in html


def test_html_embeds_data_fields():
    html = CoverageReportGenerator().generate_html(_sample_report())
    assert '"skill_name": "my-skill"' in html
    assert '"安全事件升级"' in html
    assert '"covered_by": []' in html


def test_html_script_passes_node_check():
    html = CoverageReportGenerator().generate_html(_sample_report())
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "no <script> block"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(m.group(1)); js = f.name
    r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
    assert r.returncode == 0, f"node --check failed:\n{r.stderr}"


def test_html_js_renders_gap_row_highlighted():
    html = CoverageReportGenerator().generate_html(_sample_report())
    m = re.search(r"<script>(.*)</script>", html, re.S)
    harness = (
        "var __rendered = '';\n"
        "var document = { getElementById: function () {"
        "  return { set innerHTML(v) { __rendered = v; }, get innerHTML() { return __rendered; } };"
        " } };\n" + m.group(1) + "\nprocess.stdout.write(__rendered);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness); js = f.name
    r = subprocess.run(["node", js], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    assert "安全事件升级" in r.stdout
    assert "漏测" in r.stdout
    assert 'class="gap"' in r.stdout          # 锁死行高亮逻辑(漏测行 <tr class="gap">)


def test_html_write_to_disk(tmp_path):
    p = tmp_path / "sub" / "coverage-report.html"   # 父目录不存在 → write_html 建
    CoverageReportGenerator().write_html(_sample_report(), p)
    assert p.exists()
    assert "/*__EMBEDDED_DATA__*/" not in p.read_text(encoding="utf-8")
