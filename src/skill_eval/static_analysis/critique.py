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
from skill_eval.utils import call_claude

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


def _build_critique_prompt(name: str, body: str) -> str:
    rules = "\n".join(
        f"{cn} {r[0]} —— {r[2]}\n   好例:{r[3]}  反例:{r[4]}"
        for cn, r in zip("①②③④", _RUBRIC)
    )
    return f"""你是 Claude Code Skill 的正文内容质量评审。给「读者反馈」,不打分。
你的角色是读者:读到抽象/像废话/没演示的地方指出来,读到具体可执行的点也认可。
反馈要像跟同事随口说一句,用大白话,**别写报告、别长篇大论**。

## 待评审 Skill:{name} 的 SKILL.md 正文
{body}

## 四条评审原则(逐条判断 status:met 满足 / partial 部分 / missing 缺失)
{rules}

## 任务
对每条输出:
- key: 上面的英文 key
- status: met / partial / missing
- evidence: SKILL.md 正文里支撑判断的**原文片段,数组**——每个不同位置一条(分属不同段落就拆成多条),每条只摘关键句、**别整段抄**;missing 给空数组 []
- feedback: **认可 / 已做到的大白话,1 句**。met → 好在哪(点出具体哪一处);partial → 已经做到的部分;missing → 空字符串 ""
- gap: **缺口的大白话,1-2 句**。met → 空字符串 "";partial/missing → 第一句说缺什么,第二句给个改的方向(可举个短例子,但只点到为止)。**绝对不要把 SKILL.md 原文整段重写出来,不要堆成长篇。**

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


def _parse_critique_response(response: str, skill_name: str) -> ContentQualityReport:
    """解析 LLM 评审响应。畸形/空 → 空 report(宁少报不乱报)。
    按 key 归位(4 条固定,不依赖 LLM 返回顺序);漏条/非法 status → 该条默认 missing。
    正则抠最大 {...} 同 coverage(fence 在大括号外被自然略过);但有效 dict 无/空 dimensions
    (如 {"error":"rate limited"}) → 4 条全 missing(非空 report,提示 LLM 跑题);完全非 dict
    (畸形 JSON)→ 空 report。与 coverage(无 dimensions 返空)刻意分歧:missing 比 silent empty 显眼。"""
    obj = None
    try:
        obj = json.loads(response)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", response, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                obj = None
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


def analyze_content_quality(skill_path: Path, *, model: str | None = None,
                            timeout: int = 120, runs: int = 1) -> ContentQualityReport:
    """SKILL.md body_text → 内容质量 report。call_claude 失败 → 空 report(诊断不炸)。

    runs=1(默认):单次调用。runs>1:跑 N 次多数投票聚合(每维 status 取多数、
    原文取首个判出多数档的 run、显 X/N 可信度),稳定性换 N× 调用开销。

    取 parse_skill_md 第 5 返回值 body_text(纯正文,不含 frontmatter description)——
    4 条 rubric 评正文,description 留给 trigger/static_analysis。
    """
    name, _desc, _content, _fm, body = parse_skill_md(skill_path)
    prompt = _build_critique_prompt(name, body)
    start = time.perf_counter()

    if runs <= 1:
        usage: dict = {}
        try:
            response = call_claude(prompt, model=model, timeout=timeout, on_usage=usage.update)
        except RuntimeError:
            return ContentQualityReport(skill_name=name, duration_seconds=time.perf_counter() - start)
        rep = _parse_critique_response(response, name)
        rep.token_usage = usage
        rep.duration_seconds = time.perf_counter() - start
        return rep

    # 多 run:逐次跑(顺序,避限流),成功的进 reports
    snaps: list[dict] = []
    reports: list[ContentQualityReport] = []
    for _ in range(runs):
        u: dict = {}
        try:
            response = call_claude(prompt, model=model, timeout=timeout, on_usage=u.update)
            reports.append(_parse_critique_response(response, name))
            snaps.append(u)
        except RuntimeError:
            continue
    if not reports:
        return ContentQualityReport(skill_name=name, duration_seconds=time.perf_counter() - start)
    agg = _aggregate_runs(reports, name)
    agg.token_usage = _sum_usage(snaps)
    agg.duration_seconds = time.perf_counter() - start
    return agg
