"""critique:LLM 判 SKILL.md 正文内容质量(4 维读者反馈态)。

一次 call_claude 喂 SKILL.md 正文(body_text),按 4 条 rubric 给 met/partial/missing
+ evidence + feedback。rubric ①②③ 来自 SkillLens(论文每维 65-66% 预测 downstream
utility),④ 示例驱动是经验自加(无数据验证)。
"""
import json
import re
import time
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

## 待评审 Skill:{name} 的 SKILL.md 正文
{body}

## 四条评审原则(逐条判断 status:met 满足 / partial 部分 / missing 缺失)
{rules}

## 任务
对每条输出:
- key: 上面的英文 key
- status: met / partial / missing
- evidence: 引用 SKILL.md 正文里支撑你判断的原文片段(missing 可空)
- feedback: 读者反馈。met → 写好在哪里;partial/missing → 写怎么改(尽量给具体补法)

输出 JSON ONLY,不要其他文字:
{{"dimensions":[{{"key":"failure_mechanism","status":"partial","evidence":"...","feedback":"..."}},{{"key":"actionable_specificity","status":"met","evidence":"...","feedback":"..."}},{{"key":"high_risk_blacklist","status":"missing","evidence":"","feedback":"..."}},{{"key":"examples","status":"missing","evidence":"","feedback":"..."}}]}}"""


def _parse_critique_response(response: str, skill_name: str) -> ContentQualityReport:
    """解析 LLM 评审响应。畸形/空 → 空 report(宁少报不乱报)。
    按 key 归位(4 条固定,不依赖 LLM 返回顺序);漏条/非法 status → 该条默认 missing。
    正则抠最大 {...}(fence 在大括号外被自然略过)对齐 coverage._parse_coverage_response。"""
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
        if key not in by_key and not feedback:
            feedback = "LLM 未评审该条"
        dims.append(CritiqueDimension(
            key=key, name=_NAMES[key], status=status,
            evidence=str(d.get("evidence", "")).strip(), feedback=feedback,
        ))
    return ContentQualityReport(skill_name=skill_name, dimensions=dims)
