"""coverage grid:LLM 判 evals.json 对 SKILL.md 内容点的覆盖。

一次 call_claude 喂 SKILL.md 正文 + 各 eval(id/prompt/rationale),输出内容点清单 +
每点被哪几道 eval 覆盖;covered_by 空 = 漏测。rationale 是自由文本,靠 LLM 语义映射,
不建硬结构化 instruction_ref(理由见 handbook §4.2,记忆 sift-no-structured-skillmd-same-root)。
"""
import json
import re
import time
from pathlib import Path

from sift.models import EvalSet
from sift.models.coverage_report import CoveragePoint, CoverageReport
from sift.parser import parse_skill_md
from sift.utils import call_claude


def _build_coverage_prompt(name: str, description: str, content: str, eval_set: EvalSet,
                           kind: str | None = None) -> str:
    is_agent = kind == "agent"
    role_word = "AI agent" if is_agent else "Claude Code Skill"
    def_head = "## Agent system prompt" if is_agent else "## Skill definition (SKILL.md)"
    body_word = "agent 定义正文" if is_agent else "SKILL.md"
    eval_lines = []
    for e in eval_set.evals:
        rat = (e.rationale or "").strip() or "(无 rationale)"
        eval_lines.append(f'{e.id}. prompt: "{e.prompt}" | rationale: "{rat}"')
    evals_block = "\n".join(eval_lines) if eval_lines else "(无 eval)"
    return f"""You are auditing eval coverage for a {role_word}.

Name: {name}
Description: {description}

{def_head}
{content}

## Existing evals (id / prompt / rationale)
{evals_block}

## Task
List the TESTABLE instruction points in the {body_word} — each concrete rule / step /
capability / conditional branch an eval could verify (NOT prose or examples). For each:
- label: short name
- source_quote: the verbatim {body_word} phrase it comes from
- covered_by: eval ids (from the list above) that VERIFY this point; [] if none

Be strict: a point is "covered" only if an eval actually exercises it, not merely mentions it.
Output JSON ONLY, no other text:
{{"points": [{{"label": "...", "source_quote": "...", "covered_by": [1]}}]}}"""


def _parse_coverage_response(response: str, skill_name: str) -> CoverageReport:
    """解析 LLM 覆盖映射响应。畸形/空 → 空 report(不炸,漏测诊断宁少报不乱报)。
    正则抠最大 {...}(fence 在大括号外被自然略过)对齐 generator._parse_llm_response。"""
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
    if not isinstance(obj, dict):
        return CoverageReport(skill_name=skill_name)
    raw_points = obj.get("points")
    if not isinstance(raw_points, list):
        return CoverageReport(skill_name=skill_name)
    points = []
    for rp in raw_points:
        if not isinstance(rp, dict):
            continue
        covered = rp.get("covered_by", [])
        if not isinstance(covered, list):
            covered = []
        ids = []
        for i in covered:
            try:
                ids.append(int(i))
            except (TypeError, ValueError):
                continue
        points.append(CoveragePoint(
            label=str(rp.get("label", ""))[:200],
            source_quote=str(rp.get("source_quote", "")),
            covered_by=ids,
        ))
    return CoverageReport(skill_name=skill_name, points=points)


def analyze_coverage(skill_path: Path, eval_set: EvalSet, *,
                     model: str | None = None, timeout: int = 120,
                     kind: str | None = None) -> CoverageReport:
    """一次 LLM:声明正文 + evals → 覆盖映射 report。call_claude 失败 → 空 report(诊断不炸)。

    kind='agent' 时 prompt 用 agent 措辞(内容点提取吃 parse 后 body,skill/agent 通用)。
    """
    name, description, content, _fm, _body = parse_skill_md(skill_path)
    prompt = _build_coverage_prompt(name, description, content, eval_set, kind=kind)
    usage: dict = {}
    start = time.perf_counter()
    try:
        response = call_claude(prompt, model=model, timeout=timeout, on_usage=usage.update)
    except RuntimeError:
        return CoverageReport(skill_name=name, duration_seconds=time.perf_counter() - start)
    rep = _parse_coverage_response(response, name)
    rep.token_usage = usage
    rep.duration_seconds = time.perf_counter() - start
    return rep
