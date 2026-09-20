"""--changed-from resolver:diff 两 skill 指令 → LLM 判受影响 eval id(路 A)。

回归探针:**不存** eval↔instruction 映射表,跑时 LLM 当场判。失败三分类(调用/解析/
id 不存在)抛 SiftError;空集返 [](合法:改的是与所有题无关的措辞)。

详见 docs/superpowers/specs/2026-07-28-changed-from-selective-rerun-design.md。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sift.compliance.extractor import InstructionExtractor
from sift.errors import SiftError
from sift.llm_transport import LLMCallConfig
from sift.parser import parse_skill_md
from sift.utils import call_claude

_RECOVER = "可重试,或改用 --eval-ids 指定,或去掉 --changed-from 跑全量。"


def _parse_ids(response: str, valid: set[int]) -> list[int]:
    """解析 LLM 回答为受影响 eval id 列表(保序去重)。三类失败抛 SiftError。

    - 空/白空 → 失败(返回空);
    - 非 JSON / 不含 affected_ids 列表 / 含非整数 → 失败(解析不出);
    - id 不在 valid → 失败(胡说),报错列出可用。
    """
    if not response or not response.strip():
        raise SiftError(f"--changed-from 判定失败:LLM 返回空。{_RECOVER}")
    try:
        data = json.loads(response)
    except json.JSONDecodeError as _caught_error:
        m = re.search(r"\{.*\}", response, re.DOTALL)
        if not m:
            raise SiftError(f"--changed-from 判定失败:LLM 回答解析不出 JSON。{_RECOVER}") from _caught_error
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError as exc:
            raise SiftError(f"--changed-from 判定失败:LLM 回答 JSON 解析失败({exc})。{_RECOVER}") from exc
    raw = data.get("affected_ids") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise SiftError(f"--changed-from 判定失败:LLM 未返回 affected_ids 列表。{_RECOVER}")
    try:
        ids = [int(x) for x in raw]
    except (TypeError, ValueError) as exc:
        raise SiftError(f"--changed-from 判定失败:affected_ids 含非整数({exc})。{_RECOVER}") from exc
    bad = [i for i in ids if i not in valid]
    if bad:
        raise SiftError(f"--changed-from 判定失败:LLM 返回不存在的 eval id {bad}(可用: {sorted(valid)})。{_RECOVER}")
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if not (i not in seen):
            continue
        seen.add(i)
        out.append(i)
    return out


def _skill_body(skill_dir: Path, label: str) -> str:
    """读 SKILL.md 正文(parse_skill_md 返回第 5 项 body_text)。读不出 → SiftError。"""
    try:
        return parse_skill_md(skill_dir)[4]
    except Exception as exc:  # SkillParseError 等
        raise SiftError(f"{label} 读不出 SKILL.md 正文: {exc}") from exc


def _build_prompt(baseline_inst, current_inst, evals) -> str:
    """喂给 LLM 的选题 prompt。偏过召(拿不准就带)。"""
    b = "\n".join(f"- [{i.source.value}|{i.section}] {i.instruction_text}" for i in baseline_inst) or "(无)"
    c = "\n".join(f"- [{i.source.value}|{i.section}] {i.instruction_text}" for i in current_inst) or "(无)"
    e = (
        "\n".join(f"- id={ev.id} | {ev.prompt} | expects: {'; '.join(ev.expectations) or '(无)'}" for ev in evals)
        or "(无)"
    )
    return f"""你是 sift 的回归选题器。当前 skill 相对基线改了一些指令,判断哪些 eval 受这些改动影响。

判断原则——**偏过召**:拿不准某道 eval 是否受影响就带上(宁多跑不漏跑);与改动明确无关的不带。

--- 基线 skill 指令 ---
{b}

--- 当前 skill 指令 ---
{c}

--- eval 集 ---
{e}

只输出 JSON,不要任何解释或 markdown 围栏:{{"affected_ids": [3, 5]}}
"""


def compute_affected_evals(
    current_skill_dir: Path,
    baseline_skill_dir: Path,
    eval_set: "object",
    model: str | None,
    *,
    timeout: int = 120,
) -> list[int]:
    """diff 两 skill 指令 → LLM 判受影响 eval id(路 A)。

    抽两边 SKILL.md 指令(InstructionExtractor,纯正则)+ 一次 LLM 判定。
    失败三分类(调用/解析/id 不存在)抛 SiftError;**空集返 []**(合法,
    改的是与所有题无关的措辞,由调用方决定跑空退出)。
    """
    baseline_body = _skill_body(baseline_skill_dir, "--changed-from 基线")
    current_body = _skill_body(current_skill_dir, "当前 skill")
    baseline_inst = InstructionExtractor().extract(baseline_body)
    current_inst = InstructionExtractor().extract(current_body)
    prompt = _build_prompt(baseline_inst, current_inst, eval_set.evals)
    try:
        response = call_claude(prompt=prompt, config=LLMCallConfig(model=model, timeout=timeout, max_turns=1))
    except RuntimeError as exc:
        raise SiftError(f"--changed-from 判定失败:LLM 调用出错({exc})。{_RECOVER}") from exc
    return _parse_ids(response, valid={e.id for e in eval_set.evals})
