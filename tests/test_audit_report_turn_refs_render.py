"""json 含 turn_refs;html finding 行显示 turn 定位;空 turn_refs 不崩。

Task 5:报告渲染 turn_refs。
- JSON:pydantic ``model_dump_json`` 自动序列化 turn_refs(自 T3 加字段起即如此,本测验证,无需自定义序列化)。
- HTML:模板是**自包含 JS 渲染**(数据 ``model_dump`` 嵌入 ``EMBEDDED_DATA``,浏览器跑 ``renderFinding``)。
  pytest 不跑浏览器 JS,故按 ``test_audit_viewer`` 既有惯例(参 noise 徽章测)断两层:
  ① 模板定义渲染片段(``function turnBadge``,改前 grep=0 = 真 RED 锚点);
  ② 数据流入(``turn_refs`` 随 model_dump 嵌入页面供 JS 消费)。
  运行时渲染正确性靠 node --check + 肉眼(项目既有做法)。
- 空 turn_refs(段级缺席):``write_html`` 不崩 + 数据嵌入为 []。
"""
import json

from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)
from skill_eval.reporting.audit_viewer import AuditReportGenerator


def _report_with_fail(turn_refs):
    return AuditReport(
        skill_name="wf",
        transcripts=["s.jsonl"],
        summary={"total": 1, "pass": 0, "fail": 1, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="do X",
                source="HEADING_STEP",
                verdict=AuditVerdict.FAIL,
                category=AuditCategory.STEP,
                evidence="e",
                method=AuditMethod.LLM,
                transcript="s.jsonl",
                turn_refs=turn_refs,
            )
        ],
        warnings=[],
    )


def test_json_serializes_turn_refs():
    """pydantic model_dump_json 自动带 turn_refs(无需自定义序列化)。"""
    r = _report_with_fail([142, 156])
    d = json.loads(r.model_dump_json())
    assert d["findings"][0]["turn_refs"] == [142, 156]


def test_html_template_renders_turn_refs_and_embeds_data():
    """HTML 模板定义 turn_refs 渲染片段 + 数据嵌入 EMBEDDED_DATA。

    pytest 不执行浏览器 JS,故断言分两层(同 test_audit_viewer 噪声徽章测惯例):
    ① 模板结构:定义 ``turnBadge`` 渲染函数(RED 锚点:改前 grep=0);
    ② 数据流:turn_refs 随 model_dump 嵌入页面供 JS 消费。
    运行时渲染正确性靠 node --check + 肉眼(项目既有做法)。
    """
    r = _report_with_fail([142])
    html = AuditReportGenerator().generate_html(r)
    # ① 模板结构:turnBadge 渲染函数存在(RED 锚点:改前无)
    assert "function turnBadge" in html
    # ①b wiring 锚点:renderFinding 真调了 turnBadge(单 surface,无 cross-surface 补偿,
    #     仅断函数定义抓不住「函数在但没被调」的回归——删 call site 留 dead function 仍过)
    assert "turnBadge(f.turn_refs" in html
    # ② 数据流:142 随 model_dump 嵌入 EMBEDDED_DATA 供 JS 消费
    assert "142" in html


def test_html_segment_absence_empty_turn_refs_no_crash(tmp_path):
    """段级缺席(turn_refs=[])正常生成不崩;数据嵌入为空数组。"""
    r = _report_with_fail([])
    out = tmp_path / "rep.html"
    AuditReportGenerator().write_html(r, out)  # 不崩
    html = out.read_text(encoding="utf-8")
    # 空 turn_refs 嵌入为 [](渲染端 turnBadge 见空即返回 '',不显占位)
    assert '"turn_refs": []' in html
