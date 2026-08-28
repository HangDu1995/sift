"""suggest: 审计→改进建议的可选 LLM 后处理(--suggest 显式要,默认关)。

定位红线:**审计保持独立判决,建议是挂在审计上的可选视图**——判决不为建议变形,
建议不进任何 gate/exit code。挂在 CLI 层(report 生成后、落盘前),不进 audit()
引擎(run 内 compliance 直调引擎、不经这里,零影响)。

三类归因纪律(归因决定出不出建议):
- ``skill_fix``:声明写了、执行系统性没做到 → 改 skill 正文的建议;
- ``declaration_fix``:执行做了更合理的事、声明过时/过严 → 改声明的建议;
- ``model_env``:单发失败 / 工具噪声 / 环境问题 → **不出建议别硬凑**,折叠计数透明展示
  (不从「没被建议引用的 FAIL」反推计数——LLM 漏归因 ≠ 归因为模型)。

归因粒度 = **逐指令簇**:一条 attribution = 一条 by_instruction 指令,target_seqs =
其全部 FAIL finding 的 #N(合成 finding 无聚合行,逐条一簇)。粒度定死 LLM 不自选
——计数稳、渲染不重复、输出规模有界。分批 ≤10 簇/批(http forced tool_use 输出帽
4096 硬顶,几十簇一次调用必超帽;skill_body + PASS 清单每批全量,典型审计单次调用)。

``build_suggestions`` **永不向外抛**(挂点在落盘前,抛了毁整份报告):一切失败走
skipped_reason;**无 FAIL 是正常终态 → 返回 None 不落盘**(同 trigger 无失败不留
产物;「未跑:无 FAIL」是把正常态包装成例外态)。数据完整度门:UNRESOLVED 占比
(summary.total 分母,INDETERMINATE 不进分子——重跑无益)>30% 不跑,提示先重跑。
"""
from __future__ import annotations

import json
import time

from sift.concurrency import _LLMRetryExhausted, call_llm_with_retry
from sift.models.audit_models import (
    AuditReport,
    AuditSuggestResult,
    AuditSuggestion,
    AuditVerdict,
)
from sift.scoring._jsonschema import inline_schema

# 簇/批上限:批判官同款(10/批)。skill_body + PASS 清单每批全量(压缩后很小),FAIL 簇分批。
_CLUSTERS_PER_BATCH = 10
# 数据完整度门:UNRESOLVED 占比超此值不跑(证据残缺时建议必乱)。分母 = summary.total
# (全部 findings);INDETERMINATE 不进分子(判不准、重跑无益,不是重跑能救回的残缺)。
_MAX_UNRESOLVED_RATE = 0.30
_TIMEOUT_DEFAULT = 600  # 同 trigger build_llm_suggestion(audit CLI 无 --timeout 参数)

_WIRE_CATEGORIES = ("skill_fix", "declaration_fix", "model_env")

# wire schema ≠ 落盘模型:LLM 输出走 attributions[],category enum 锁形(forced tool_use
# 下 schema 即约束,自由字符串一条坏值会经 catch-all 毒死全部建议);required 最小集 =
# {category, target_seqs, rationale}——suggestion/keeps_pass 对 fix 两类必填由**代码侧**
# 校验(JSON Schema 表达不了按 category 的条件必填),缺 → 整条丢弃 + dropped_count
# (不硬凑);model_env 条目忽略建议字段。
_WIRE_SCHEMA = inline_schema({
    "type": "object",
    "properties": {
        "attributions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(_WIRE_CATEGORIES),
                        "description": "skill_fix=改 skill 正文 / declaration_fix=改声明 / model_env=模型或环境问题(不出建议)",
                    },
                    "target_seqs": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "该簇全部 FAIL finding 的 #N(逐指令簇:一条 attribution 对应一个簇)",
                    },
                    "rationale": {"type": "string", "description": "归因理由(为何属这类)"},
                    "suggestion": {"type": "string", "description": "可直接照做的修改建议(skill_fix/declaration_fix 必填;model_env 留空)"},
                    "keeps_pass": {"type": "boolean", "description": "自检:建议改的段落不与任何 PASS 指令撞车"},
                    "keeps_pass_note": {"type": "string", "description": "一句核对说明"},
                },
                "required": ["category", "target_seqs", "rationale"],
            },
        },
    },
    "required": ["attributions"],
})


def _tokens_of(usage: dict) -> int:
    """usage 里 input/output 之和(非数字字段跳过——mock/payload 偶带非数值键)。"""
    n = 0
    for k in ("input_tokens", "output_tokens"):
        v = usage.get(k, 0)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            n += v
    return n


def _pass_list(report: AuditReport) -> list[str]:
    """PASS 指令清单(保留账本用):从 by_instruction 取(fail_count==0 且 pass_count>0)。

    排除 derived(聚合行无 derived 标志,按 findings 的 derived 文本集反查);合成 finding
    不进 by_instruction 天然排除。回退链 headline > source_excerpt > instruction_text
    (headline 可能空:--no-headline / 缓存 miss)。
    """
    derived_texts = {f.instruction_text for f in report.findings if f.derived_from_transcript}
    out: list[str] = []
    for row in report.by_instruction:
        if row.fail_count == 0 and row.pass_count > 0 and row.instruction_text not in derived_texts:
            out.append(row.headline or row.source_excerpt or row.instruction_text)
    return out


def _fail_clusters(report: AuditReport) -> list[dict]:
    """FAIL findings 按逐指令簇分组:真声明指令按 instruction_text 一簇,合成逐条一簇。

    每簇带 by_instruction 行(系统性/单发信号:fail_count/total/fail_in_transcripts/
    unresolved_count)+ 簇内 findings(seq/evidence/method/noise/synthetic)。
    """
    by_text = {row.instruction_text: row for row in report.by_instruction}
    clusters: dict[str, dict] = {}
    order: list[str] = []
    for f in report.findings:
        if f.verdict != AuditVerdict.FAIL:
            continue
        # 合成 finding 无聚合行、同文案跨 transcript 也不是同一声明指令 → 逐条一簇
        key = f"\0synth\0{f.transcript}\0{f.seq}" if f.synthetic else f.instruction_text
        if key not in clusters:
            clusters[key] = {"findings": [], "row": by_text.get(f.instruction_text)}
            order.append(key)
        clusters[key]["findings"].append(f)
    return [clusters[k] for k in order]


def _cluster_block(i: int, cluster: dict, n_transcripts: int) -> str:
    """单个簇的 prompt 块(逐指令簇的载体:概要 + 簇内每条 FAIL finding 的事实)。"""
    row = cluster["row"]
    lines = [f"### 簇 {i}"]
    for f in cluster["findings"]:
        tag = " [合成关系声明(顺序),建议须指向两端步骤的原文]" if f.synthetic else ""
        noise = f" · 噪声判别:{f.noise_label}(工具噪声,不得据其出 skill_fix 建议)" if f.noise_label == "tool_noise" else ""
        lines.append(
            f"- finding #{f.seq} [{f.method.value}]{tag}{noise} evidence: {f.evidence}"
        )
    if row is not None:
        lines.append(
            f"- 指令:{row.headline or row.source_excerpt or row.instruction_text}\n"
            f"  跨会话:失败 {row.fail_count}/{row.total} · 失败于 "
            f"{', '.join(row.fail_in_transcripts) or '(未记录)'} · 未判成 {row.unresolved_count}"
            f"(transcript 总数 {n_transcripts})"
        )
    return "\n".join(lines)


def _prompt(skill_body: str, pass_list: list[str], clusters: list[dict], n_transcripts: int) -> str:
    single_note = (
        "\n注意:本次 transcript 总数为 1——「单发失败」不作为 model_env 的依据"
        "(每条都是 1/1),归因交给 evidence / 程序判定 / 指令清晰度。\n"
        if n_transcripts <= 1 else ""
    )
    pass_section = "\n".join(f"- {p}" for p in pass_list) or "- (无)"
    blocks = "\n\n".join(_cluster_block(i, c, n_transcripts) for i, c in enumerate(clusters))
    return (
        "你在帮一个 skill 作者归因审计发现的违规并起草改进建议。审计 = 把 skill 声明的"
        "指令与真实执行 transcript 逐条对账;FAIL = 声明要求了但执行没做到(或判定了违反)。\n\n"
        "## 三类归因(每个簇归一类,归因决定出不出建议;一簇一条 attribution,target_seqs"
        " 填该簇全部 FAIL finding 的 #N)\n"
        "- skill_fix:声明写了、执行**系统性**没做到(多会话重复失败 / 程序判定确定 /"
        " 指令清晰无歧义)→ suggestion 给出**改 skill 正文**的可照做建议(改哪段、怎么改"
        "措辞/加约束/加步骤)。\n"
        "- declaration_fix:执行做了**更合理**的事、声明过时或过严(evidence 显示行为正确"
        "但违反字面,如跳过了冗余步骤)→ suggestion 给出**改声明**的建议(把正确实践写回"
        "声明)。\n"
        "- model_env:单发失败(失败会话数 < transcript 总数)/ 工具噪声 / 环境问题 →"
        " **不出建议别硬凑**(category=model_env,suggestion 留空)。\n"
        f"{single_note}\n"
        f"## skill 声明全文\n\"\"\"\n{skill_body}\n\"\"\"\n\n"
        "## 已通过(PASS)的指令清单(保留账本:你的建议不得与它们冲突——若建议改的段落"
        "恰是某条 PASS 指令,keeps_pass 填 false 并在 keeps_pass_note 说明)\n"
        f"{pass_section}\n\n"
        f"## 待归因的违规簇({len(clusters)} 簇)\n{blocks}\n\n"
        "## 输出\n"
        '只返回 JSON:{"attributions": [{"category": "skill_fix|declaration_fix|model_env", '
        '"target_seqs": [3, 7], "rationale": "归因理由", "suggestion": "修改建议'
        '(skill_fix/declaration_fix 必填;model_env 留空)", "keeps_pass": true, '
        '"keeps_pass_note": "一句核对说明"}]}'
    )


def _parse_attributions(resp: str) -> list[dict] | None:
    """解析 LLM 的 attributions(wire 对象);空响应/解析失败 → None(调用方记 dropped/skipped)。"""
    text = (resp or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return None
    items = data.get("attributions")
    return items if isinstance(items, list) else None


def build_suggestions(
    report: AuditReport,
    skill_body: str,
    model: str | None = None,
    timeout: int = _TIMEOUT_DEFAULT,
) -> AuditSuggestResult | None:
    """对 report 的 FAIL findings 做三类归因并起草建议(CLI 层,--suggest 显式要才调)。

    - **无 FAIL → None 不落盘**(正常终态,同 trigger 无失败不留产物);
    - UNRESOLVED 占比 > 30% → 空结果 + skipped_reason(证据残缺,先重跑);
    - 分批 ≤10 簇/批;**永不向外抛**(挂点在落盘前):重试耗尽 → skipped_reason 收尾
      (保留已成功批的建议);整批空响应/解析失败 → 该批簇计 dropped_count 继续;
    - 归因映射:fix 两类须非空 suggestion(缺 → 整条丢 + dropped_count);
      model_env 折叠计数 = Σ target_seqs 去重(= FAIL 条数,与文案单位一致);
      target_seqs 引用不存在 #N → 剔除该 seq,全无效 → 丢条目。
    """
    fail_findings = [f for f in report.findings if f.verdict == AuditVerdict.FAIL]
    if not fail_findings:
        return None
    total = report.summary.get("total", 0) or len(report.findings)
    unresolved = report.summary.get("unresolved", 0)
    if total and unresolved / total > _MAX_UNRESOLVED_RATE:
        return AuditSuggestResult(skipped_reason=(
            f"UNRESOLVED 占比 {unresolved}/{total} > 30%,证据残缺——先重跑补齐未判成条目,再要建议"
        ))

    start = time.perf_counter()
    usage: dict = {}
    clusters = _fail_clusters(report)
    max_seq = max((f.seq for f in report.findings), default=0)
    pass_list = _pass_list(report)
    n_transcripts = len(report.transcripts)

    suggestions: list[AuditSuggestion] = []
    model_env_seqs: set[int] = set()
    dropped = 0
    skipped_reason = ""
    try:
        for bstart in range(0, len(clusters), _CLUSTERS_PER_BATCH):
            batch = clusters[bstart:bstart + _CLUSTERS_PER_BATCH]
            prompt = _prompt(skill_body, pass_list, batch, n_transcripts)
            resp = call_llm_with_retry(
                prompt, model=model, timeout=timeout,
                schema=_WIRE_SCHEMA, on_usage=usage.update, transport="http",
            )
            items = _parse_attributions(resp)
            if items is None:
                # 整批没解出来:该批簇透明计数(继续跑其余批,别让一批毁全部)
                dropped += len(batch)
                continue
            for item in items:
                if not isinstance(item, dict):
                    dropped += 1
                    continue
                category = item.get("category")
                if category not in _WIRE_CATEGORIES:
                    dropped += 1
                    continue
                seqs = [
                    s for s in (item.get("target_seqs") or [])
                    if isinstance(s, int) and not isinstance(s, bool) and 1 <= s <= max_seq
                ]
                if not seqs:
                    dropped += 1
                    continue
                if category == "model_env":
                    model_env_seqs.update(seqs)
                    continue
                text = str(item.get("suggestion") or "").strip()
                if not text:
                    dropped += 1  # fix 类缺 suggestion:不硬凑(归因纪律在 wire 层的落实)
                    continue
                suggestions.append(AuditSuggestion(
                    category=category,
                    target_seqs=seqs,
                    suggestion=text,
                    rationale=str(item.get("rationale") or ""),
                    keeps_pass=bool(item.get("keeps_pass", False)),
                    keeps_pass_note=str(item.get("keeps_pass_note") or ""),
                ))
    except _LLMRetryExhausted:
        skipped_reason = "LLM 调用重试耗尽(疑似限流)"
    except Exception as exc:  # catch-all:挂点在落盘前,永不向外抛
        skipped_reason = f"建议生成失败: {str(exc)[:120]}"

    return AuditSuggestResult(
        suggestions=suggestions,
        model_env_count=len(model_env_seqs),
        dropped_count=dropped,
        skipped_reason=skipped_reason,
        tokens=_tokens_of(usage),
        duration_seconds=time.perf_counter() - start,
    )
