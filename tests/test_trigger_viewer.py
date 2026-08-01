"""trigger HTML 报告生成器单测(镜像 test_audit_viewer)。"""

import re
import subprocess
import tempfile

from skill_eval.models.trigger_report import ProbeResult, Suggestion, TriggerReport
from skill_eval.reporting.trigger_viewer import TriggerReportGenerator


def _sample_report(with_suggestions=True, with_fail=True):
    probes = [
        ProbeResult(id=1, expect_trigger=True, prompt="分析这份日志",
                    trigger_rate=0.0 if with_fail else 1.0,
                    verdict="FAIL" if with_fail else "PASS", tokens=842, duration_seconds=4.5),
        ProbeResult(id=2, expect_trigger=False, prompt="翻译小说",
                    trigger_rate=0.0, verdict="PASS", tokens=318, duration_seconds=2.1),
    ]
    suggestions = [
        Suggestion(category="recall", source="rule",
                   message="漏召:补关键词\n  · probe-1:「分析这份日志」", probe_ids=[1]),
        Suggestion(category="llm_rewrite", source="llm",
                   message="修订 description:做日志分析\n理由:补了关键词", probe_ids=[1]),
    ] if with_suggestions else []
    return TriggerReport(
        skill_name="my-skill", description="分析日志找异常", description_hash="a1b2c3d4e5f60718",
        runs=2, recall=0.0 if with_fail else 1.0, false_trigger_rate=0.0,
        n_should_trigger=1, n_should_not=1, unresolved=0,
        token_usage={"total_tokens": 28400,
                     "suggest": {"input_tokens": 200, "output_tokens": 80, "total_cost_usd": 0.02}},
        duration_seconds=12.3,
        suggestions=suggestions, probes=probes,
    )


def test_html_replaces_placeholder_and_has_root():
    html = TriggerReportGenerator().generate_html(_sample_report())
    assert "/*__EMBEDDED_DATA__*/" not in html          # 占位符已注入
    assert 'id="trigger-root"' in html                  # 结构锚点


def test_html_embeds_data_fields():
    html = TriggerReportGenerator().generate_html(_sample_report())
    assert '"skill_name": "my-skill"' in html
    assert '"description": "分析日志找异常"' in html
    assert '"prompt": "分析这份日志"' in html
    assert '"category": "recall"' in html
    assert '"llm_rewrite"' in html
    assert '"suggest"' in html                          # token_usage.suggest
    assert '"duration_seconds": 12.3' in html           # 耗时字段进了 embedded JSON
    assert '"tokens": 842' in html                      # per-probe token 进了 embedded JSON


def test_html_renders_with_no_suggestions():
    """suggestions 为空也能渲染(不缺字段、不崩)。"""
    html = TriggerReportGenerator().generate_html(_sample_report(with_suggestions=False, with_fail=False))
    assert "/*__EMBEDDED_DATA__*/" not in html
    assert 'id="trigger-root"' in html


def test_html_write_to_disk(tmp_path):
    p = tmp_path / "sub" / "trigger-report.html"        # 父目录不存在 → write_html 建
    TriggerReportGenerator().write_html(_sample_report(), p)
    assert p.exists()
    assert "/*__EMBEDDED_DATA__*/" not in p.read_text(encoding="utf-8")


def test_html_script_passes_node_check():
    """<script> 抽出跑 node --check(记忆条硬要求:括号失衡/replace 逻辑/路径错配 render-smoke 抓不到)。"""
    html = TriggerReportGenerator().generate_html(_sample_report())
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "no <script> block"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(m.group(1))
        js = f.name
    r = subprocess.run(["node", "--check", js], capture_output=True, text=True)
    assert r.returncode == 0, f"node --check failed:\n{r.stderr}"


def test_html_js_renders_badges_and_per_probe_usage():
    """端到端验 JS 渲染:node 里跑 <script>,注入 EMBEDDED_DATA + document stub,断言渲染出的
    innerHTML 含 汇总耗时徽章 + per-probe 消耗列(tokens + 耗时)(验读取路径 + 格式化,render-smoke 抓不到)。"""
    html = TriggerReportGenerator().generate_html(_sample_report())
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "no <script> block"
    harness = (
        "var __rendered = '';\n"
        "var document = { getElementById: function () {"
        "  return { set innerHTML(v) { __rendered = v; }, get innerHTML() { return __rendered; } };"
        " } };\n"
        + m.group(1) + "\nprocess.stdout.write(__rendered);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        js = f.name
    r = subprocess.run(["node", js], capture_output=True, text=True)
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    assert "耗时" in r.stdout and "12.3s" in r.stdout     # 汇总耗时徽章(fmtDur)
    assert "842 tok" in r.stdout and "4.5s" in r.stdout   # per-probe 消耗列
