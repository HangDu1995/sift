from sift.models.audit_models import (
    AuditReport, AuditFinding, AuditMethod, AuditVerdict, AuditCategory, InstructionAggregate,
)
from sift.reporting.audit_viewer import AuditReportGenerator
import json
import re
import subprocess


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


def test_audit_html_renders_token_usage_when_present():
    report = AuditReport(
        skill_name="s",
        transcripts=["t.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[],
        by_instruction=[],
        warnings=[],
        token_usage={"input_tokens": 1234, "output_tokens": 56,
                     "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0,
                     "total_cost_usd": 0.0234, "calls": 3},
    )
    html = AuditReportGenerator().generate_html(report)
    # JS 在浏览器渲染 span,test 核数据落盘 + 模板含渲染逻辑(渲染输出由 node -e 验)
    assert '"token_usage"' in html          # embedded JSON 带字段
    assert '"calls": 3' in html             # 值落盘
    assert "tu.calls" in html               # 模板含读取 token 的渲染逻辑


def test_audit_html_omits_token_usage_when_empty():
    report = AuditReport(
        skill_name="s",
        transcripts=["t.jsonl"],
        summary={"total": 1, "pass": 1, "fail": 0, "na": 0},
        findings=[],
        by_instruction=[],
        warnings=[],
        # token_usage 默认 {} → tu.calls 假 → 浏览器不渲染 span
    )
    html = AuditReportGenerator().generate_html(report)
    assert '"token_usage": {}' in html      # 空字段落盘


def test_audit_html_renders_warnings_and_findings_full_set():
    report = AuditReport(
        skill_name="s",
        transcripts=["t1.jsonl"],
        summary={"total": 4, "pass": 1, "fail": 1, "na": 1, "unresolved": 1},
        findings=[
            AuditFinding(
                instruction_text="必须引用行号", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="证据-FAIL-未见行号引用", method=AuditMethod.LLM, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="反推的指令", source="REQUIRED_READS",
                verdict=AuditVerdict.NA, category=AuditCategory.CONDITIONAL,
                evidence="证据-NA-场景不适用", method=AuditMethod.LLM, transcript="t1.jsonl",
                derived_from_transcript=True,
            ),
            AuditFinding(
                instruction_text="没判成的指令", source="CONSTRAINT",
                verdict=AuditVerdict.UNRESOLVED, category=AuditCategory.PROHIBITION,
                evidence="证据-UNRESOLVED-LLM 没判成", method=AuditMethod.LLM, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="读 app.log", source="REQUIRED_READS",
                verdict=AuditVerdict.PASS, category=AuditCategory.CONDITIONAL,
                evidence="证据-PASS-命中 Read app.log", method=AuditMethod.PROGRAMMATIC, transcript="t1.jsonl",
            ),
        ],
        by_instruction=[],
        warnings=["1 条指令没判成(UNRESOLVED:LLM 调用/解析失败/未提及)(本次结果可能不完整,建议重跑)"],
    )
    html = AuditReportGenerator().generate_html(report)
    # 防假绿:evidence/warnings 文本作为 JSON 嵌入一定在 html 里,断言它们只能证明"数据注入了",
    # 证明不了"JS 渲染了"。所以**有效 RED 锚点是结构标记**:
    assert 'id="audit-warnings"' in html
    assert 'id="audit-findings"' in html
    # 数据存在性(辅助,非渲染验证):
    assert "1 条指令没判成(UNRESOLVED" in html
    assert "证据-UNRESOLVED-LLM 没判成" in html  # UNRESOLVED(没判成)态也要覆盖
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
                evidence="t1 未见核实", method=AuditMethod.LLM, transcript="t1.jsonl",
            ),
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="t2 未见核实", method=AuditMethod.LLM, transcript="t2.jsonl",
            ),
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.PASS, category=AuditCategory.PROHIBITION,
                evidence="t3 有核实", method=AuditMethod.PROGRAMMATIC, transcript="t3.jsonl",
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


def test_audit_html_renders_noise_badges_on_fail_findings_and_aggregate():
    """FAIL finding + by_instruction 失败行加噪声判别徽章(Task 8)。

    source/category 徽章 finding-head 已有(本测试覆盖重点在新增 noise 徽章):
    - finding-head:FAIL 且 noise_label → 噪声徽章(疑似工具噪声 / skill 问题 / 待确认);
    - by_instruction 违规表加「噪声判别」列。
    RED 锚点:模板定义 noiseBadge 函数 + 列头「噪声判别」(改前 grep=0);label 字面量在 map。
    运行时正确性靠 node --check(test_audit_cli)+ 肉眼;pytest 验模板 JS 源引用。
    """
    report = AuditReport(
        skill_name="s",
        transcripts=["t1.jsonl", "t2.jsonl"],
        summary={"total": 2, "pass": 0, "fail": 2, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                evidence="ev", method=AuditMethod.LLM, transcript="t1.jsonl",
                noise_label="tool_noise", noise_reasons=["evidence 自相矛盾"],
            ),
            AuditFinding(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                verdict=AuditVerdict.PASS, category=AuditCategory.PROHIBITION,
                evidence="ev2", method=AuditMethod.PROGRAMMATIC, transcript="t2.jsonl",
            ),
        ],
        by_instruction=[
            InstructionAggregate(
                instruction_text="NEVER 编造", source="CONSTRAINT",
                category=AuditCategory.PROHIBITION, pass_count=1, fail_count=1,
                na_count=0, total=2, fail_rate=0.5,
                fail_in_transcripts=["t1.jsonl"], fail_seqs=[1],
                noise_label="uncertain", noise_reasons=["分布无显著噪声特征"],
            ),
        ],
        warnings=[],
    )
    html = AuditReportGenerator().generate_html(report)
    # RED 锚点:模板定义 noiseBadge 函数 + 违规表加「噪声判别」列头(改前无)
    assert "function noiseBadge" in html
    assert "噪声判别" in html
    # label 字面量在 NOISE_LABELS map(三态全列)
    assert "疑似工具噪声" in html
    assert "疑似 skill 问题" in html
    assert "待确认" in html
    # 数据流:finding 级 + 聚合级 noise_label 随 model_dump 嵌入
    assert '"noise_label": "tool_noise"' in html      # finding 级
    assert '"noise_label": "uncertain"' in html       # 聚合级
    # 违规表渲染(fail_count>0) + finding-head 渲染(FAIL finding)
    assert 'id="audit-by-instruction-fail"' in html
    assert 'id="audit-findings-grouped"' in html


def test_audit_html_quantity_badge_on_control_flow_findings(tmp_path):
    """control_flow==='quantity' 的 finding 挂「数量」徽章——数量 finding 按设计沿用
    原指令原文/原类别(对聚合透明),不挂徽章在报告里认不出是数量判定(实测连作者都要
    翻 evidence 找);ORDER 靠 synthetic + 「(顺序)」文案自明,不另标。
    RED 锚点:模板读 f.control_flow、qty 徽章 class(改前 grep=0);label 字面量「数量」。
    运行时正确性靠 node --check(test_audit_cli)+ 肉眼;pytest 验模板 JS 源引用。
    """
    report = AuditReport(
        skill_name="s",
        transcripts=["t1.jsonl"],
        summary={"total": 2, "pass": 0, "fail": 2, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="调用 health-check 至少 2 次", source="TOOL_REQUIREMENT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.CONDITIONAL,
                evidence="实测调用 health-check 1 次,不满足「至少 2 次」",
                method=AuditMethod.PROGRAMMATIC, transcript="t1.jsonl",
                control_flow="quantity",
            ),
            AuditFinding(
                instruction_text="(顺序)「A」须先于「B」", source="HEADING_STEP",
                verdict=AuditVerdict.FAIL, category=AuditCategory.STEP,
                evidence="B 的全部证据先于 A", method=AuditMethod.LLM,
                transcript="t1.jsonl", synthetic=True,
            ),
        ],
        warnings=[],
    )
    html = AuditReportGenerator().generate_html(report)
    # RED 锚点:模板判断 + 徽章 class(改前无)
    assert "f.control_flow" in html
    assert 'class="qty"' in html
    # 数据流:control_flow 随 model_dump 嵌入(HTML 徽章的读数来源)
    assert '"control_flow": "quantity"' in html
    # 指令行也挂徽章(renderInstruction):只低头读指令的人不看 finding 头徽章簇也认得出
    # (此 report 无 by_instruction → 命中只能来自 ④ 明细的指令行,非概要表)
    m = _render_by_instruction_via_node(report, tmp_path)
    assert m["qtyAny"] is True
    assert m["qtyInstr"] is True


def test_audit_html_quantity_badge_on_by_instruction_fail_rows(tmp_path):
    """概要表(③ 违规 FAIL 行)同样挂「数量」徽章——数量 finding 沿用原指令原文,
    概览表不挂记号认不出是控制流判定(与 CLI 终端违规表的 (数量) 前缀同一信息)。
    隔离验证:finding 不带 control_flow、聚合行带 → 徽章只能来自表行渲染路径。"""
    from sift.models.audit_models import InstructionAggregate

    report = AuditReport(
        skill_name="s", transcripts=["t1.jsonl"],
        summary={"total": 1, "pass": 0, "fail": 1, "na": 0},
        findings=[
            AuditFinding(
                instruction_text="调用 health-check 至少 2 次", source="TOOL_REQUIREMENT",
                verdict=AuditVerdict.FAIL, category=AuditCategory.CONDITIONAL,
                evidence="实测调用 1 次", method=AuditMethod.PROGRAMMATIC,
                transcript="t1.jsonl", seq=1,
            ),
        ],
        by_instruction=[
            InstructionAggregate(
                instruction_text="调用 health-check 至少 2 次", source="TOOL_REQUIREMENT",
                category=AuditCategory.CONDITIONAL, pass_count=0, fail_count=1,
                na_count=0, unresolved_count=0, indeterminate_count=0, total=1,
                fail_rate=1.0, fail_in_transcripts=["t1.jsonl"], fail_seqs=[1],
                control_flow="quantity",
            ),
        ],
        warnings=[],
    )
    html = AuditReportGenerator().generate_html(report)
    assert "a.control_flow" in html          # 表行渲染读聚合行字段(RED 锚点)
    assert '"control_flow": "quantity"' in html
    # CSS 不限定 .finding-head(改前 .finding-head .qty 只管 ④ 明细,概要表格子里的
    # span 无样式 → 退化成纯文本挤在指令开头,看着像句子一部分、不像记号)
    assert ".qty {" in html
    assert ".finding-head .qty" not in html
    m = _render_by_instruction_via_node(report, tmp_path)
    assert m["qtyAny"] is True               # finding 未带标记 → 徽章来自概要表行


def _by_instruction_report(transcripts):
    """单/多 transcript + by_instruction(各 1 fail 行 + 1 na 行)的审计报告,供 ③ 渲染测。"""
    return AuditReport(
        skill_name="s", transcripts=transcripts,
        summary={"total": 2, "pass": 0, "fail": 1, "na": 1},
        findings=[
            AuditFinding(instruction_text="NEVER 编造", source="CONSTRAINT",
                         verdict=AuditVerdict.FAIL, category=AuditCategory.PROHIBITION,
                         evidence="ev", method=AuditMethod.LLM, transcript=transcripts[0]),
            AuditFinding(instruction_text="Use ref: x.md", source="REFERENCE_USAGE",
                         verdict=AuditVerdict.NA, category=AuditCategory.CONDITIONAL,
                         evidence="ev-na", method=AuditMethod.LLM, transcript=transcripts[0]),
        ],
        by_instruction=[
            InstructionAggregate(
                instruction_text="NEVER 编造", source="CONSTRAINT", category=AuditCategory.PROHIBITION,
                pass_count=0, fail_count=1, na_count=0, total=len(transcripts),
                fail_rate=1.0 / len(transcripts), fail_in_transcripts=transcripts[:1], fail_seqs=[1],
                noise_label="uncertain", noise_reasons=["r"]),
            InstructionAggregate(
                instruction_text="Use ref: x.md", source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL,
                pass_count=0, fail_count=0, na_count=len(transcripts), total=len(transcripts),
                fail_rate=0.0, fail_in_transcripts=[],
                na_in_transcripts=transcripts, na_seqs=[2]),
        ],
        warnings=[],
    )


def _render_by_instruction_via_node(report, tmp_path):
    """抽 audit-report.html 的 <script>,在 node 里 mock document 跑真渲染,返回 ③ 表的列标记。

    列裁剪(单 transcript 去 fail%/fail/total/in transcripts)是 JS 运行时字符串变换,
    render-smoke 只验模板字符串抓不到(记忆 sift-viewer-js-needs-node-check)。
    故 mock DOM 实跑 IIFE,收 root.insertAdjacentHTML 的产物断言。
    """
    html = AuditReportGenerator().generate_html(report)
    assert "multi && (EMBEDDED_DATA.by_instruction" not in html   # 门控已放开(不再要求 multi)
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL))
    preamble = (
        "var __c=[];var __els=[];function FN(){this._k=[];this._h='';__els.push(this);}"
        "Object.defineProperty(FN.prototype,'innerHTML',{set:function(v){this._h=v;},get:function(){return this._h;}});"
        "FN.prototype.insertAdjacentHTML=function(p,h){__c.push(h);};"
        "FN.prototype.appendChild=function(c){this._k.push(c);return c;};"
        "FN.prototype.setAttribute=function(){};"
        "FN.prototype.addEventListener=function(){};"
        "var __m={'audit-root':new FN()};"
        "var document={getElementById:function(i){if(!__m[i])__m[i]=new FN();return __m[i];},"
        "createElement:function(){return new FN();}};\n"
    )
    # o = insertAdjacentHTML 产物 + createElement 元素的 innerHTML(findingRow 走
    # createElement+innerHTML+appendChild,只收前者会漏掉 ④ 明细的渲染产物)。
    epilogue = (
        "\nvar o=__c.concat(__els.map(function(e){return e._h;})).join('\\n');"
        "console.log(JSON.stringify({"
        "failPct:o.includes('fail%'),failTotal:o.includes('fail/total'),"
        "inTrans:o.includes('in transcripts'),failTbl:o.includes('违规 FAIL'),"
        "naTbl:o.includes('场景不适用 N/A'),kua:o.includes('跨 '),"
        "covTbl:o.includes('声明覆盖(coverage)'),covOverall:o.includes('总覆盖'),"
        "covUncovered:o.includes('未覆盖'),covSection:o.includes('声明分组-规则段'),"
        "covSeqLink:o.includes('#finding-'),"
        "bndTbl:o.includes('工具对账(boundary'),bndUndeclared:o.includes('>未声明'),"
        "bndDeclared:o.includes('>已声明'),bndHint:o.includes('不构成违规判定'),"
        "bndNoDeclNote:o.includes('声明未列任何工具'),"
        "sugBlock:o.includes('id=\"audit-suggest\"'),"
        "sugGroup:o.includes('改 skill 建议'),"
        "sugEnv:o.includes('归因为模型/环境问题'),"
        "sugReason:o.includes('改进建议(未生成)'),"
        "sugSeqLink:o.includes('#finding-2'),"
        "sugDropped:o.includes('模型输出不合规已丢弃'),"
        "stageTbl:o.includes('阶段耗时 / token'),"
        "stageSug:o.includes('>建议</td>'),"
        "stageJudgeCf:o.includes('顺序对/数量'),"
        "stagePct67:o.includes('(67%)'),"
        "stagePct200:o.includes('(200%)'),"
        "qtyAny:o.includes('class=\"qty\"'),"
        "qtyInstr:o.includes('数量</span> 调用 health-check 至少 2 次')}));"
    )
    runfile = tmp_path / "run.js"
    runfile.write_text(preamble + js + epilogue, encoding="utf-8")
    out = subprocess.run(["node", str(runfile)], capture_output=True, text=True)
    assert out.returncode == 0, f"node runtime failed:\n{out.stderr}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_audit_html_by_instruction_renders_for_single_transcript_drops_cross_session_cols(tmp_path):
    """单 transcript ③ 也显违规/NA 概要表,但去掉跨会话列(fail%/fail/total/in transcripts)、标题不带「跨」。

    列裁剪是运行时变换 → 走 node mock DOM 实跑(见 _render_by_instruction_via_node)。
    """
    m = _render_by_instruction_via_node(_by_instruction_report(["t1.jsonl"]), tmp_path)
    assert m["failTbl"] is True           # 违规 FAIL 表仍显
    assert m["naTbl"] is True             # 场景不适用 N/A 表仍显
    assert m["kua"] is False              # 单 transcript 标题不带「跨 N transcript」
    assert m["failPct"] is False          # 跨会话列 fail% 去掉
    assert m["failTotal"] is False        # fail/total 去掉(单 transcript 恒 1/1,无意义)
    assert m["inTrans"] is False          # "in transcripts" 列去掉


def test_audit_html_by_instruction_multi_transcript_keeps_cross_session_cols(tmp_path):
    """多 transcript ③ 保留全部跨会话列(回归护栏,防把多 transcript 也裁了)。"""
    m = _render_by_instruction_via_node(_by_instruction_report(["t1.jsonl", "t2.jsonl"]), tmp_path)
    assert m["failTbl"] is True
    assert m["naTbl"] is True
    assert m["kua"] is True               # 多 transcript 标题带「跨 N transcript」
    assert m["failPct"] is True           # 跨会话列保留
    assert m["failTotal"] is True
    assert m["inTrans"] is True


def _coverage_report(transcripts, with_uncovered=True):
    """带声明覆盖视图的审计报告:① covered 行 ② uncovered 行(uncovered>0 时)。

    覆盖区块的数据门是 cov.total > 0——by_instruction 可为空(覆盖块独立于 ③)。
    """
    from sift.models.audit_models import CoverageSummary, InstructionCoverage, SectionCoverage

    instructions = [
        InstructionCoverage(
            instruction_text="必须先读输入", source="CONSTRAINT",
            category=AuditCategory.PROHIBITION, section="声明分组-规则段",
            status="covered", pass_count=len(transcripts), total=len(transcripts), seqs=[1],
        ),
    ]
    if with_uncovered:
        instructions.append(
            InstructionCoverage(
                instruction_text="发布前必须跑回归", source="CONSTRAINT",
                category=AuditCategory.PROHIBITION, section="声明分组-规则段",
                status="uncovered", na_count=len(transcripts), total=len(transcripts), seqs=[2],
            )
        )
    n = len(instructions)
    uncovered = n - 1 if with_uncovered else 0
    return AuditReport(
        skill_name="s", transcripts=transcripts,
        summary={"total": 2, "pass": 1, "fail": 0, "na": 1},
        findings=[], by_instruction=[], warnings=[],
        coverage=CoverageSummary(
            total=n, covered=1, uncovered=uncovered, unknown=0, coverage_rate=1 / n,
            sections=[
                SectionCoverage(
                    section="声明分组-规则段", total=n, covered=1, uncovered=uncovered,
                    unknown=0, coverage_rate=1 / n, instructions=instructions,
                )
            ],
        ),
    )


def test_audit_html_coverage_block_structural_markers():
    """③b 声明覆盖:结构标记(RED 锚点)+ 数据落盘 + 渲染门控存在。"""
    html = AuditReportGenerator().generate_html(_coverage_report(["t1.jsonl"]))
    assert 'id="audit-coverage"' in html          # 结构标记(渲染逻辑存在)
    assert "声明覆盖" in html                      # 区块标题字面量
    assert "cov && cov.total" in html             # 数据门(total=0 不渲染)
    assert '"coverage"' in html                   # embedded JSON 带字段
    assert '"covered": 1' in html                 # 值落盘


def test_audit_html_coverage_renders_for_single_and_multi_transcript(tmp_path):
    """覆盖块单/多 transcript 都渲染(covered=任一,无 multi 分支);
    #N 链接(→ ④ 明细锚点)也在渲染产物里。"""
    for transcripts in (["t1.jsonl"], ["t1.jsonl", "t2.jsonl"]):
        m = _render_by_instruction_via_node(_coverage_report(transcripts), tmp_path)
        assert m["covTbl"] is True
        assert m["covOverall"] is True
        assert m["covUncovered"] is True
        assert m["covSection"] is True            # 真实分组标题原样显示
        assert m["covSeqLink"] is True            # #finding-N 链接


def test_audit_html_coverage_hidden_when_total_zero(tmp_path):
    """total=0(空声明/旧产物默认 CoverageSummary)→ 覆盖块不渲染。"""
    from sift.models.audit_models import CoverageSummary

    report = AuditReport(
        skill_name="s", transcripts=["t1.jsonl"],
        summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
        findings=[], by_instruction=[], warnings=[],
        coverage=CoverageSummary(),
    )
    m = _render_by_instruction_via_node(report, tmp_path)
    assert m["covTbl"] is False
    assert m["covOverall"] is False


# ---- ①b 阶段耗时/token 表(suggest 行)----

def test_audit_html_stage_table_suggest_row_pct_of_whole_pipeline(tmp_path):
    """suggest 行(--suggest 真跑过 LLM 才由 CLI 追加)入阶段表;% 占整条流水线
    (引擎墙钟 + 建议耗时)——不是只占引擎墙钟,否则建议行 >100%、其余行失真。
    顺带钉死 judge 行 hover 描述含控制流判官(判定行的消耗口径)。"""
    report = AuditReport(
        skill_name="s", transcripts=["t1.jsonl"],
        summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
        findings=[], by_instruction=[], warnings=[],
        duration_seconds=40.0,
        stage_breakdown=[
            {"stage": "judge", "seconds": 40.0, "input_tokens": 100, "output_tokens": 50,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "calls": 3, "note": ""},
            {"stage": "suggest", "seconds": 80.0, "input_tokens": 900, "output_tokens": 300,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "calls": 4, "note": ""},
        ],
    )
    m = _render_by_instruction_via_node(report, tmp_path)
    assert m["stageTbl"] is True
    assert m["stageSug"] is True          # 「建议」行渲染(标签/类型/描述映射补齐)
    assert m["stageJudgeCf"] is True      # 判定行描述含「控制流:顺序对/数量」
    assert m["stagePct67"] is True        # 80/(40+80)=67%:分母含建议耗时
    assert m["stagePct200"] is False      # 不是 80/40=200%(旧分母=引擎墙钟)


# ---- ③c 越界视图(boundary,一期信息性)----

def _boundary_report(transcripts, with_declared=True):
    """带越界视图的报告:① undeclared 行(主体)② domain 行(声明命中)。"""
    from sift.models.audit_models import BoundarySummary, ToolUsageEntry

    tools = [
        ToolUsageEntry(name="mcp__db__query", classification="undeclared-domain",
                       sessions=len(transcripts), calls=3, transcripts=list(transcripts)),
    ]
    if with_declared:
        tools.append(ToolUsageEntry(
            name="mcp__lint__check", classification="domain",
            declared_by="用 mcp__lint__check 做静态检查", sessions=1, calls=1,
            transcripts=transcripts[:1]))
    return AuditReport(
        skill_name="s", transcripts=transcripts,
        summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
        findings=[], by_instruction=[], warnings=[],
        boundary=BoundarySummary(
            total_tools=len(tools),
            declared=1 if with_declared else 0,
            undeclared=1,
            tools=tools,
        ),
    )


def test_audit_html_boundary_block_structural_markers():
    """③c 越界视图:结构标记 + 数据落盘 + 渲染门控存在。"""
    html = AuditReportGenerator().generate_html(_boundary_report(["t1.jsonl"]))
    assert 'id="audit-boundary"' in html        # 结构锚点
    assert "工具对账" in html                    # 区块标题字面量
    assert "bnd && bnd.total_tools" in html     # 数据门(total_tools=0 不渲染)
    assert '"boundary"' in html                 # embedded JSON 带字段
    assert '"undeclared-domain"' in html        # 值落盘


def test_audit_html_boundary_renders_rows_and_disclaimer(tmp_path):
    """有声明命中 + 未声明 → 表格行(未声明/已声明徽章)+「不构成违规判定」免责声明。"""
    for transcripts in (["t1.jsonl"], ["t1.jsonl", "t2.jsonl"]):
        m = _render_by_instruction_via_node(_boundary_report(transcripts), tmp_path)
        assert m["bndTbl"] is True
        assert m["bndUndeclared"] is True       # undeclared-domain 行的「未声明」徽章
        assert m["bndDeclared"] is True         # domain 行的「已声明」徽章
        assert m["bndHint"] is True             # 免责声明(信息性,不构成违规判定)


def test_audit_html_boundary_hidden_when_total_tools_zero(tmp_path):
    """total_tools=0(全 builtin / 旧产物默认 BoundarySummary)→ 区块不渲染。"""
    from sift.models.audit_models import BoundarySummary

    report = AuditReport(
        skill_name="s", transcripts=["t1.jsonl"],
        summary={"total": 0, "pass": 0, "fail": 0, "na": 0},
        findings=[], by_instruction=[], warnings=[],
        boundary=BoundarySummary(),
    )
    m = _render_by_instruction_via_node(report, tmp_path)
    assert m["bndTbl"] is False
    assert m["bndHint"] is False


def test_audit_html_boundary_no_declaration_note(tmp_path):
    """declared=0 且有工具 → 头部显「声明未列任何工具」防误读(非轨迹越界)。"""
    m = _render_by_instruction_via_node(_boundary_report(["t1.jsonl"], with_declared=False), tmp_path)
    assert m["bndTbl"] is True
    assert m["bndNoDeclNote"] is True
    assert m["bndDeclared"] is False           # 无 domain 行 → 无「已声明」徽章


# ---- ⑥ 改进建议区块(--suggest 产;渲染优先级 skipped_reason 先行) --------------


def _suggest_report(result) -> AuditReport:
    return AuditReport(
        skill_name="s", transcripts=["t1.jsonl"],
        summary={"total": 2, "pass": 0, "fail": 2, "na": 0},
        findings=[], by_instruction=[], warnings=[],
        suggestions=result,
    )


def test_audit_html_suggest_renders_groups_env_count_and_links(tmp_path):
    """有建议 → 分组(改 skill/改声明)+ model_env 计数行 + #N 可点跳链接。"""
    from sift.models.audit_models import AuditSuggestResult, AuditSuggestion

    result = AuditSuggestResult(
        suggestions=[
            AuditSuggestion(category="skill_fix", target_seqs=[2],
                            suggestion="加显式约束", rationale="系统性", keeps_pass=True),
            AuditSuggestion(category="declaration_fix", target_seqs=[1],
                            suggestion="改为可选", rationale="声明过时", keeps_pass=False),
        ],
        model_env_count=3,
        dropped_count=1,
    )
    m = _render_by_instruction_via_node(_suggest_report(result), tmp_path)
    assert m["sugBlock"] is True
    assert m["sugGroup"] is True
    assert m["sugEnv"] is True            # 「N 条 FAIL 归因为模型/环境问题,不出建议」
    assert m["sugSeqLink"] is True        # target_seqs → #finding-N 链接
    assert m["sugDropped"] is True        # dropped 透明一行


def test_audit_html_suggest_hidden_when_none(tmp_path):
    """suggestions=None(默认关/无 FAIL)→ 区块不渲染。"""
    m = _render_by_instruction_via_node(_suggest_report(None), tmp_path)
    assert m["sugBlock"] is False


def test_audit_html_suggest_zero_attribution_hidden(tmp_path):
    """跑成但零归因(全空计数、reason 空)→ 正常态不渲染空视图(同 boundary/coverage)。"""
    from sift.models.audit_models import AuditSuggestResult

    m = _render_by_instruction_via_node(_suggest_report(AuditSuggestResult()), tmp_path)
    assert m["sugBlock"] is False


def test_audit_html_suggest_skipped_reason_only_no_env_count(tmp_path):
    """skipped_reason 非空 → 只显原因行、不显 model_env 计数(失败 ≠ 全怪模型)。"""
    from sift.models.audit_models import AuditSuggestResult

    result = AuditSuggestResult(skipped_reason="UNRESOLVED 占比过高,先重跑", model_env_count=5)
    m = _render_by_instruction_via_node(_suggest_report(result), tmp_path)
    assert m["sugBlock"] is True
    assert m["sugReason"] is True
    assert m["sugEnv"] is False           # 计数行不显(渲染优先级写死)
