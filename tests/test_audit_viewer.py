from skill_eval.models.audit_models import (
    AuditReport, AuditFinding, AuditVerdict, AuditCategory, InstructionAggregate,
)
from skill_eval.reporting.audit_viewer import AuditReportGenerator


def test_audit_html_renders_header_skill_name_and_transcripts():
    report = AuditReport(
        skill_name="code-review-auditor",
        transcripts=["run-1/transcript.jsonl"],
        summary={"total": 3, "pass": 1, "fail": 1, "na": 1},
        findings=[],
        by_instruction=[],
        warnings=[],
    )
    html = AuditReportGenerator().generate_html(report)
    assert "code-review-auditor" in html
    assert "run-1/transcript.jsonl" in html
    assert "/*__EMBEDDED_DATA__*/" not in html
    assert 'id="audit-root"' in html


def test_audit_html_renders_warnings_and_findings_full_set():
    report = AuditReport(
        skill_name="s",
        transcripts=["t1.jsonl"],
        summary={"total": 4, "pass": 1, "fail": 1, "na": 1, "unclear": 1},
        findings=[
            AuditFinding(
                instruction_text="必须引用行号", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="证据-FAIL-未见行号引用", confidence=0.9, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="反推的指令", source="REQUIRED_READS",
                verdict=AuditVerdict.NA, category=AuditCategory.CONDITIONAL,
                evidence="证据-NA-场景不适用", confidence=0.8, transcript="t1.jsonl",
                derived_from_transcript=True,
            ),
            AuditFinding(
                instruction_text="判不准的指令", source="CONSTRAINT",
                verdict=AuditVerdict.UNCLEAR, category=AuditCategory.PROHIBITION,
                evidence="证据-UNCLEAR-LLM 没判成", confidence=0.3, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="读 app.log", source="REQUIRED_READS",
                verdict=AuditVerdict.PASS, category=AuditCategory.CONDITIONAL,
                evidence="证据-PASS-命中 Read app.log", confidence=1.0, transcript="t1.jsonl",
            ),
        ],
        by_instruction=[],
        warnings=["1 条指令因 LLM 判定失败标为判不准(UNCLEAR)(本次结果可能不完整,建议重跑)"],
    )
    html = AuditReportGenerator().generate_html(report)
    # 防假绿:evidence/warnings 文本作为 JSON 嵌入一定在 html 里,断言它们只能证明"数据注入了",
    # 证明不了"JS 渲染了"。所以**有效 RED 锚点是结构标记**:
    assert 'id="audit-warnings"' in html
    assert 'id="audit-findings"' in html
    # 数据存在性(辅助,非渲染验证):
    assert "1 条指令因 LLM 判定失败标为判不准(UNCLEAR)" in html
    assert "证据-UNCLEAR-LLM 没判成" in html  # UNCLEAR(判不准)第 4 态也要覆盖
    assert "证据-PASS-命中 Read app.log" in html  # PASS 也要(全集,§50 核心)


def test_audit_html_renders_by_instruction_and_groups_findings_multi_transcript():
    report = AuditReport(
        skill_name="s",
        transcripts=["t1.jsonl", "t2.jsonl", "t3.jsonl"],
        summary={"total": 6, "pass": 2, "fail": 3, "na": 1},
        findings=[
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="t1 未见核实", confidence=0.9, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="t2 未见核实", confidence=0.9, transcript="t2.jsonl",
            ),
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.PASS, category=AuditCategory.PROHIBITION,
                evidence="t3 有核实", confidence=1.0, transcript="t3.jsonl",
            ),
        ],
        by_instruction=[
            InstructionAggregate(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                category=AuditCategory.PROHIBITION, pass_count=1, fail_count=2,
                na_count=0, total=3, fail_rate=2 / 3,
                fail_in_transcripts=["t1.jsonl", "t2.jsonl"],
                fail_seqs=[2, 5],
            ),
            InstructionAggregate(
                instruction_text="Use ref: security-policy.md", source="REFERENCE_USAGE",
                category=AuditCategory.CONDITIONAL, pass_count=0, fail_count=0,
                na_count=2, total=2, fail_rate=0.0,
                fail_in_transcripts=[],
                na_in_transcripts=["t1.jsonl", "t2.jsonl"],
                na_seqs=[8, 9],
            ),
        ],
        warnings=[],
    )
    html = AuditReportGenerator().generate_html(report)
    # 拆两张表:违规 FAIL(fail>0)+ 场景不适用 N/A(na>0),模板里各有独立 id/标题/表头
    # (静态 render-smoke 只验模板结构 + 数据流;行内容如 2/2、明细 #N 是 JS 运行时算的,
    #  源码里没有,运行时正确性靠 node --check + 读 JS + 终端并行实现已运行时测过)
    assert 'id="audit-by-instruction-fail"' in html   # 违规表(fail_count>0 → 渲染)
    assert 'id="audit-by-instruction-na"' in html     # 场景不适用表(na_count>0 → 渲染)
    assert '违规 FAIL' in html
    assert '场景不适用 N/A' in html
    assert html.count('<th>明细 #</th>') >= 2         # 两张表各有明细 # 列(交叉引用到明细)
    assert 'na/total' in html                         # N/A 表头列(模板字面量)
    assert 'na_in_transcripts' in html                # 字段随 model_dump 注入页面,供 JS 消费
    assert 'fail_seqs' in html and 'na_seqs' in html  # 明细 #N 数据流入页面
    assert 'id="audit-findings-grouped"' in html
    # 单 transcript 的扁平容器在多 transcript 下不应出现(分组了)
    assert 'id="audit-findings"' not in html or 'id="audit-findings-grouped"' in html
