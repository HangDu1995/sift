"""critique:LLM 判 SKILL.md 正文内容质量(4 维读者反馈态)。

一次 call_claude 喂 SKILL.md 正文(body_text),按 4 条 rubric 给 met/partial/missing
+ evidence + feedback。rubric ①②③ 来自 SkillLens(论文每维 65-66% 预测 downstream
utility),④ 示例驱动是经验自加(无数据验证)。
"""
import json
import re
import time
from collections import Counter
from pathlib import Path

from skill_eval.models.critique_report import ContentQualityReport, CritiqueDimension
from skill_eval.parser import parse_skill_md
from skill_eval.utils import call_claude, _HttpRateLimited

# (key, 中文名, 定义, 好例, 反例)
_RUBRIC: list[tuple[str, str, str, str, str]] = [
    ("failure_mechanism", "失败机制编码",
     "有没有写「agent 具体会栽的具体坑」+ 为什么栽(失败条件 + 因果链)。",
     "「API 分页上限 100,不翻页会静默丢数据」", "「处理错误要小心」"),
    ("actionable_specificity", "可执行具体性",
     "步骤够不够照着能干,引用真实的工具/API/对象名。",
     "「改完重新查对象拿真 ID,再用这个 ID 调用」", "「把任务拆小」"),
    ("high_risk_blacklist", "高风险黑名单",
     "有没有明确拉黑「看起来对但其实会害死人」的操作,带具体原因。",
     "「别直接拿用户串当正则——. * 会让匹配静默失败,先转义」", "「注意危险操作」"),
    ("examples", "示例驱动",
     "有没有 worked example(完整演示一遍),而非纯抽象规则。",
     "一段真实命令序列 / 真实输入→真实输出", "只有规则没有演示"),
]
_KEYS = [r[0] for r in _RUBRIC]
_NAMES = {r[0]: r[1] for r in _RUBRIC}
_VALID_STATUS = {"met", "partial", "missing"}

# forced tool_use 的 inline schema(无 $ref——GLM 等 Anthropic 兼容端点不解析 tool input_schema
# 的 $ref,必填字段是 $ref 时模型返 {})。key/status 用枚举锁形状,evidence/feedback/gap 自由文本。
_CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": _KEYS},
                    "status": {"type": "string", "enum": ["met", "partial", "missing"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "feedback": {"type": "string"},
                    "gap": {"type": "string"},
                },
                "required": ["key", "status", "evidence", "feedback", "gap"],
            },
        }
    },
    "required": ["dimensions"],
}

# http 裸调用的有界重试:GLM forced tool_use 偶发返空 tool_use({})/撞 429,实锤多为瞬态,
# 同 prompt 重试常解出(镜像 audit _judge_batch_with_retry 的"调用失败 / 解析为空"重试)。
_RETRY_MAX = 2         # 空响应 / 调用失败 最多重试 2 次(共 3 次尝试)
_RETRY_DELAY = 1.0     # 线性退避(秒 × 尝试序号)

# prompt 措辞按对象选准词:skill 分支冻结成现状(已 shipped,字节不变不造回归);
# agent 分支换准词(不把 agent.md 叫 SKILL.md)。4 维 rubric 两边共用,不动。
# body_ref=「X 正文」(标题/evidence 槽),orig_ref=「X 原文」(禁整段重写槽,原文用「原文」非「正文」)。
_SUBJECT_TERMS = {
    "skill": {"role": "Claude Code Skill", "label": "Skill",
              "body_ref": "SKILL.md 正文", "orig_ref": "SKILL.md 原文"},
    "agent": {"role": "Claude Code agent", "label": "agent",
              "body_ref": "agent 定义正文", "orig_ref": "agent 定义原文"},
}


def _build_critique_prompt(name: str, body: str, subject: str = "skill") -> str:
    _t = _SUBJECT_TERMS.get(subject, _SUBJECT_TERMS["skill"])
    role, label, body_ref, orig_ref = _t["role"], _t["label"], _t["body_ref"], _t["orig_ref"]
    rules = "\n".join(
        f"{cn} {r[0]} —— {r[2]}\n   好例:{r[3]}  反例:{r[4]}"
        for cn, r in zip("①②③④", _RUBRIC)
    )
    return f"""你是 {role} 的正文内容质量评审。给「读者反馈」,不打分。
你的角色是读者:读到抽象/像废话/没演示的地方指出来,读到具体可执行的点也认可。
反馈要像跟同事随口说一句,用大白话,**别写报告、别长篇大论**。

## 待评审 {label}:{name} 的 {body_ref}
{body}

## 四条评审原则(逐条判断 status:met 满足 / partial 部分 / missing 缺失)
{rules}

## 任务
对每条输出:
- key: 上面的英文 key
- status: met / partial / missing
- evidence: {body_ref}里支撑判断的**原文片段,数组**——每个不同位置一条(分属不同段落就拆成多条),每条只摘关键句、**别整段抄**;missing 给空数组 []
- feedback: **认可 / 已做到的大白话,1 句**。met → 好在哪(点出具体哪一处);partial → 已经做到的部分;missing → 空字符串 ""
- gap: **缺口的大白话,1-2 句**。met → 空字符串 "";partial/missing → 第一句说缺什么,第二句给个改的方向(可举个短例子,但只点到为止)。**绝对不要把 {orig_ref}整段重写出来,不要堆成长篇。**

输出 JSON ONLY,不要其他文字:
{{"dimensions":[{{"key":"failure_mechanism","status":"partial","evidence":["处理错误要小心"],"feedback":"有提醒读者注意错误。","gap":"太泛了,没说具体栽在哪。补一条:某操作因某原因会失败,对应怎么查。"}},{{"key":"actionable_specificity","status":"met","evidence":["改完重新查对象拿真 ID","再用这个 ID 调用"],"feedback":"步骤具体、引用真实对象名,照着能干。","gap":""}},{{"key":"high_risk_blacklist","status":"missing","evidence":[],"feedback":"","gap":"没拉黑看起来对但会害死人的操作。补一条带原因的禁令。"}},{{"key":"examples","status":"missing","evidence":[],"feedback":"","gap":"只有规则没演示。补一段真实命令或输入→输出。"}}]}}"""


def _coerce_evidence(raw) -> list[str]:
    """LLM 返回的 evidence 归一成 list[str]:数组→逐条 strip 过滤空串;字符串→单条;其余→[]。
    容错 LLM 偶尔退回字符串形态(应数组)。"""
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _extract_json_obj(response: str) -> dict | None:
    """从 LLM 响应抠 JSON 对象:先 json.loads,失败则正则兜底最大 {...}(fence 在大括号外被略过)。
    返 dict;非 dict / 畸形 / 空 → None。_parse_critique_response 与 _response_has_content 共用。"""
    try:
        obj = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", response, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
    return obj if isinstance(obj, dict) else None


def _response_has_content(response: str) -> bool:
    """LLM 响应是否至少含一条可用 dimension 判定(dimensions 数组里有合法 key 的条目)。
    空 tool_use({}) / 空 dimensions / 全非法 key / 畸形 → False(退化,该重试)。"""
    obj = _extract_json_obj(response)
    if obj is None:
        return False
    dims = obj.get("dimensions")
    return isinstance(dims, list) and any(
        isinstance(d, dict) and d.get("key") in _NAMES for d in dims
    )


def _parse_critique_response(response: str, skill_name: str) -> ContentQualityReport:
    """解析 LLM 评审响应。畸形/空 → 空 report(宁少报不乱报)。
    按 key 归位(4 条固定,不依赖 LLM 返回顺序);漏条/非法 status → 该条默认 missing。
    正则抠最大 {...} 同 coverage(fence 在大括号外被自然略过);但有效 dict 无/空 dimensions
    (如 {"error":"rate limited"}) → 4 条全 missing(非空 report,提示 LLM 跑题);完全非 dict
    (畸形 JSON)→ 空 report。与 coverage(无 dimensions 返空)刻意分歧:missing 比 silent empty 显眼。"""
    obj = _extract_json_obj(response)
    by_key: dict[str, dict] = {}
    if isinstance(obj, dict) and isinstance(obj.get("dimensions"), list):
        for d in obj["dimensions"]:
            if isinstance(d, dict) and d.get("key") in _NAMES:
                by_key[d["key"]] = d
    if not by_key and not isinstance(obj, dict):
        return ContentQualityReport(skill_name=skill_name)   # 完全畸形 → 空
    dims = []
    for key in _KEYS:
        d = by_key.get(key, {})
        status = str(d.get("status", "")).strip().lower()
        if status not in _VALID_STATUS:
            status = "missing"
        feedback = str(d.get("feedback", "")).strip()
        gap = str(d.get("gap", "")).strip()
        if key not in by_key and not feedback and not gap:
            gap = "LLM 未评审该条"   # 漏评提示放 gap(missing 显 ✗ 行)
        dims.append(CritiqueDimension(
            key=key, name=_NAMES[key], status=status,
            evidence=_coerce_evidence(d.get("evidence")),
            feedback=feedback, gap=gap,
        ))
    return ContentQualityReport(skill_name=skill_name, dimensions=dims)


_STRICTNESS = {"missing": 3, "partial": 2, "met": 1}   # 并列时取更严(偏向提醒)


def _majority_status(statuses: list[str]) -> tuple[str, int]:
    """多数 status;并列取更严(missing>partial>met)。返 (winner, winner_count)。
    全非法 → (missing, 0)。"""
    c = Counter(s for s in statuses if s in _STRICTNESS)
    if not c:
        return "missing", 0
    best = max(c.values())
    cands = [s for s, n in c.items() if n == best]
    return max(cands, key=lambda s: _STRICTNESS[s]), best


def _sum_usage(snaps: list[dict]) -> dict:
    """多 run token 用量求和(只累加数值字段,跳过 server_tool_use/service_tier 等非数值)。"""
    out: dict = {}
    for s in snaps:
        for k, v in (s or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return out


def _aggregate_runs(reports: list[ContentQualityReport], skill_name: str) -> ContentQualityReport:
    """多 run 聚合(--runs N):每维 status 取多数(并列更严);evidence/feedback/gap 取
    **首个判出多数档的 run**(保证贴的原文与判的档自洽);vote_tally = 多数档命中数/有效 run 数。"""
    dims = []
    for key in _KEYS:
        per_run = []
        for rep in reports:
            m = [d for d in rep.dimensions if d.key == key]
            if m:
                per_run.append(m[0])
        if not per_run:
            continue
        winner, count = _majority_status([d.status for d in per_run])
        src = next((d for d in per_run if d.status == winner), per_run[0])
        dims.append(CritiqueDimension(
            key=key, name=_NAMES[key], status=winner,
            evidence=src.evidence, feedback=src.feedback, gap=src.gap,
            vote_tally=f"{count}/{len(per_run)}",
        ))
    return ContentQualityReport(skill_name=skill_name, dimensions=dims)


def _critique_call(prompt: str, model: str | None, timeout: int) -> tuple[str | None, dict]:
    """一次 critique LLM 调用(http 裸通道 + forced tool_use schema),含空响应 / 调用失败的
    有界重试。

    GLM forced tool_use 偶发返空 tool_use({})、或撞 429——实锤多为瞬态,同 prompt 重试常解出
    (镜像 audit ``_judge_batch_with_retry`` 的"调用失败 / 解析为空"重试)。529 由 ``call_claude``
    dispatcher 内部回退 cc,这里不操心。

    返 ``(response, usage)``:``response`` 非 None = 拿到至少含一条可用 dimension 的响应;
    None = 重试耗尽(调用反复失败 / 反复退化),调用方优雅降级(空 report)。``usage`` 是最后一次
    尝试的 payload(failed attempt 不回调 on_usage,degenerate attempt 的会被后续覆盖)。
    """
    usage: dict = {}
    response: str | None = None
    for attempt in range(_RETRY_MAX + 1):
        if attempt > 0:
            time.sleep(_RETRY_DELAY * attempt)
        try:
            response = call_claude(
                prompt, model=model, timeout=timeout,
                on_usage=usage.update, schema=_CRITIQUE_SCHEMA, transport="http",
            )
        except (_HttpRateLimited, RuntimeError):
            response = None
            continue
        if _response_has_content(response):
            return response, usage
        response = None           # 退化(空 tool_use / 畸形),重试
    return None, usage


def analyze_content_quality(skill_path: Path, *, model: str | None = None,
                            timeout: int = 120, runs: int = 1,
                            subject: str = "skill") -> ContentQualityReport:
    """声明正文(SKILL.md / agent 定义.md)body_text → 内容质量 report。

    走 http 裸通道(forced tool_use + inline schema,省 cc 进程 spawn + ~25k 系统 prompt);
    GLM forced tool_use 偶发空响应 / 429 由 ``_critique_call`` 有界重试兜。重试耗尽 / 调用失败
    → 空 report(诊断不炸)。runs=1(默认)单次;runs>1 多数投票聚合(每维 status 取多数、
    原文取首个判出多数档的 run、显 X/N 可信度)。

    subject=skill/agent 只选 prompt 措辞(4 维 rubric 两边共用);取 parse_skill_md 第 5
    返回值 body_text(纯正文,不含 frontmatter description)——description 留给行为层
    (skill→trigger-accuracy / agent→dispatch-accuracy),不进 critique。
    """
    name, _desc, _content, _fm, body = parse_skill_md(skill_path)
    prompt = _build_critique_prompt(name, body, subject)
    start = time.perf_counter()

    if runs <= 1:
        response, usage = _critique_call(prompt, model, timeout)
        if response is None:
            return ContentQualityReport(skill_name=name, duration_seconds=time.perf_counter() - start)
        rep = _parse_critique_response(response, name)
        rep.token_usage = usage
        rep.duration_seconds = time.perf_counter() - start
        return rep

    # 多 run:逐次跑(顺序,避限流),每次 _critique_call 自带重试;拿到内容的进 reports
    snaps: list[dict] = []
    reports: list[ContentQualityReport] = []
    for _ in range(runs):
        response, u = _critique_call(prompt, model, timeout)
        if response is None:
            continue
        reports.append(_parse_critique_response(response, name))
        snaps.append(u)
    if not reports:
        return ContentQualityReport(skill_name=name, duration_seconds=time.perf_counter() - start)
    agg = _aggregate_runs(reports, name)
    agg.token_usage = _sum_usage(snaps)
    agg.duration_seconds = time.perf_counter() - start
    return agg
