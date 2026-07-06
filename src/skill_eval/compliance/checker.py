"""Compliance checker — verifies process followed SKILL.md instructions."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from skill_eval.models import (
    CheckMethod,
    ComplianceReport,
    ComplianceResultItem,
    InstructionCheck,
    InstructionPriority,
    InstructionSource,
)
from skill_eval.utils import call_claude

_BATCH_TIMEOUT = 90
_SINGLE_TIMEOUT = 30
_MAX_LLM_IN_BATCH = 30
_APPLICABILITY_TIMEOUT = 60
# P0-1b: 禁止性词确定性高(CONSTRAINT 语义本义),不过 LLM 精筛;仅"必须"类需精筛。
_CONSTRAINT_MUST_KEYWORD = "必须"
_PROHIBITIVE_CN: tuple[str, ...] = ("禁止", "严禁", "不得", "不应", "不可")
# P0-1c: self-consistency —— 适用性判定非确定(§39 实测 filtered_count [6,6,0] 漂移),
# 单次抽签可能错。N 次采样每条候选取多数票(治漂移 + 提精度),再按 SKILL.md 内容 hash
# 缓存多数结果(同 skill 只判一次,跨 run 复用 → filtered_count 确定一致)。
_APPLICABILITY_VOTES = 3
_APPLICABILITY_CACHE: dict[str, list[bool]] = {}
_APPLICABILITY_LOCK = threading.Lock()


def clear_applicability_cache() -> None:
    """清空适用性判定缓存(测试隔离用)。"""
    with _APPLICABILITY_LOCK:
        _APPLICABILITY_CACHE.clear()


def _extract_first_balanced(text: str, open_ch: str = "[", close_ch: str = "]") -> str | None:
    """提取首个平衡的 JSON 数组/对象(字符串感知)。

    旧贪婪正则 ``\\[[\\s\\S]*\\]`` 会从首个 ``[`` 一路吞到文本**末个** ``]``。当 LLM
    在 JSON 之后的 reasoning 里用 ``[0]-[4]``/``[5]`` 引用条目(实测 GLM 常这么做),
    贪婪正则把 JSON+reasoning+尾 ``]`` 全包进来 → ``json.loads`` 失败 → 调用方默认
    全保留(适用性)或全 False(遵循),被误判成"LLM 漂移"(实为 parser bug,见探针)。

    本函数按括号深度配平取首个完整结构,且跳过 JSON 字符串内部的括号(evidence 里
    可能含 ``[N]``/``}``)。不平衡或不存在返回 None。
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


class ComplianceChecker:
    def check(
        self,
        instructions: list[InstructionCheck],
        transcript: str,
        output_dir: Path,
        model: str | None = None,
        timeout: int = 120,
        tool_trace: list | None = None,
        on_substep=None,
        log_path: Path | None = None,
    ) -> ComplianceReport:
        # P0-1b: CONSTRAINT 候选 LLM 精筛 —— 仅"必须"类(无禁止性词)过 LLM 去假阳性
        instructions, filtered_count = self._filter_constraint_candidates(instructions, model)
        total = len(instructions)
        if log_path:
            msg = f"Compliance check: {total} instructions"
            if filtered_count:
                msg += f" (filtered {filtered_count} non-applicable CONSTRAINT candidates)"
            log_path.write_text(msg + "\n", encoding="utf-8")

        llm_instructions = []
        llm_indices = []
        results: dict[int, ComplianceResultItem] = {}

        for idx, instruction in enumerate(instructions):
            if instruction.check_method == CheckMethod.LLM_JUDGE:
                llm_instructions.append(instruction)
                llm_indices.append(idx)
            else:
                item = self._check_instruction(instruction, transcript, output_dir, model, timeout, tool_trace)
                results[idx] = item
                self._log_step(log_path, idx + 1, total, instruction.instruction_text[:80], item)
                if on_substep:
                    on_substep(idx + 1, total, instruction.instruction_text[:60], item.followed, item.confidence)

        if llm_instructions:
            batch_instructions = llm_instructions[:_MAX_LLM_IN_BATCH]
            batch_indices = llm_indices[:_MAX_LLM_IN_BATCH]
            remaining_indices = llm_indices[_MAX_LLM_IN_BATCH:]

            batch_results = self._check_llm_batch(batch_instructions, transcript, model, _BATCH_TIMEOUT)

            for i, item in enumerate(batch_results):
                idx = batch_indices[i]
                results[idx] = item
                self._log_step(log_path, idx + 1, total, batch_instructions[i].instruction_text[:80], item)
                if on_substep:
                    on_substep(idx + 1, total, batch_instructions[i].instruction_text[:60], item.followed, item.confidence)

            for idx in remaining_indices:
                results[idx] = ComplianceResultItem(
                    instruction=instructions[idx],
                    followed=False,
                    evidence=f"Skipped: LLM batch budget ({_MAX_LLM_IN_BATCH}) exhausted",
                    confidence=0.0,
                    source="llm",
                )
                self._log_step(log_path, idx + 1, total, instructions[idx].instruction_text[:80], results[idx])

        items = [results[i] for i in range(total)]
        followed = sum(1 for i in items if i.followed)
        required_items = [i for i in items if i.instruction.priority == InstructionPriority.REQUIRED]
        required_followed = sum(1 for i in required_items if i.followed)

        return ComplianceReport(
            skill_name="",
            total_instructions=total,
            followed_count=followed,
            compliance_rate=followed / total if total > 0 else 0.0,
            checks=items,
            warnings=[],
            filtered_count=filtered_count,
        )

    @staticmethod
    def _log_step(log_path: Path | None, step: int, total: int, text: str, item: ComplianceResultItem):
        if not log_path:
            return
        tag = "PASS" if item.followed else "FAIL"
        line = f"[{tag}] {step}/{total} [{item.source}] conf={item.confidence:.1f} {text}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)

    def _filter_constraint_candidates(
        self, instructions: list[InstructionCheck], model: str | None
    ) -> tuple[list[InstructionCheck], int]:
        """P0-1b: CONSTRAINT 候选里,仅"必须"类(无禁止性词)过 LLM 精筛去假阳性。

        禁止性词(禁止/严禁/不得/不应/不可)确定性高直接保留;"必须"类可能是
        路由提示(必须先读某文件)/描述语(必须考虑…),过 LLM 判可遵循性。
        返回 (过滤后 instructions, 剔除数)。LLM 不可用时保守保留全部(不误剔)。
        """
        must_indices = [
            i for i, inst in enumerate(instructions)
            if inst.source == InstructionSource.CONSTRAINT
            and _CONSTRAINT_MUST_KEYWORD in inst.instruction_text
            and not any(w in inst.instruction_text for w in _PROHIBITIVE_CN)
        ]
        if not must_indices:
            return instructions, 0

        candidates = [instructions[i] for i in must_indices]
        cache_key = self._candidate_cache_key(candidates, model)
        with _APPLICABILITY_LOCK:
            applicable_mask = _APPLICABILITY_CACHE.get(cache_key)
        if applicable_mask is None:
            applicable_mask = self._llm_judge_constraint_applicability(candidates, model)
            with _APPLICABILITY_LOCK:
                _APPLICABILITY_CACHE[cache_key] = applicable_mask

        removed = {
            global_i for local_i, global_i in enumerate(must_indices)
            if not applicable_mask[local_i]
        }
        if not removed:
            return instructions, 0

        filtered = [inst for i, inst in enumerate(instructions) if i not in removed]
        return filtered, len(removed)

    @staticmethod
    def _candidate_cache_key(candidates: list[InstructionCheck], model: str | None) -> str:
        """P0-1c: 候选指令文本 + model 的 hash → 缓存键。同 SKILL.md → 同键 → 跨 run 复用。"""
        content = f"model={model or 'default'}\n" + "\n".join(
            c.instruction_text for c in candidates
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _llm_judge_constraint_applicability(
        self, candidates: list[InstructionCheck], model: str | None
    ) -> list[bool]:
        """P0-1c: self-consistency —— N 次采样每条候选取多数票。

        可遵循性判定标准不变(见下)。原本单次 call_claude 非确定(§39 实测同 skill
        三 run filtered_count [6,6,0]),单次抽签可能错。现采 N 次样,每条候选按"说
        applicable 的票数"取多数:过半(≥ samples/2,平票保守保留)→ 保留,否则剔除。
        样本不足(成功 < 半数请求)→ 保守全保留。全失败 → 退到关键词法现状(全保留)。

        可遵循:强制要求执行者做/不做某具体行为,可在执行过程里检验(如"必须使用 UTF-8"
        "必须按步骤顺序执行""必须先读 design.md 作为强制前置")。非可遵循:文档导航/概述
        (表格括号附注"(必须先读)"、章节标题"(所有类别必须)"、描述语"必须考虑和完成的"),
        不是对执行者的可检验硬性要求。返回与 candidates 等长 bool 列表(True=保留)。
        """
        n = len(candidates)
        instruction_list = "".join(
            f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(candidates)
        )
        prompt = (
            "判断以下每条指令是否为「可遵循的硬约束」(强制要求执行者做或不做某件具体行为)。\n\n"
            "## 判定标准\n"
            "- ✅ 可遵循硬约束:要求执行者执行或避免某具体动作,可在执行过程里检验是否照做。"
            "例如「必须使用 UTF-8 编码」「必须按 Step 1→2→3 顺序执行」"
            "「必须在 @triton.jit kernel 中实现」「必须先读 design.md(作为强制前置步骤时)」。\n"
            "- ❌ 非可遵循:文档导航或概述性语句,不是可检验的执行约束。例如表格单元格里的"
            "括号附注「(⚠️ 必须先读)」、章节标题「(所有类别必须)」、描述语「必须考虑和完成的」"
            "——这些是提示读者去看哪、或概述下文要讲什么,不是对执行者的硬性要求。\n\n"
            f"## 候选指令 ({n} 条)\n{instruction_list}\n"
            "## 输出格式\n"
            "只返回 JSON 数组,每元素:{\"index\": 0, \"applicable\": true/false}\n"
        )
        votes = [0] * n  # votes[i] = 说 applicable=True 的样本数
        samples = 0
        for _ in range(_APPLICABILITY_VOTES):
            try:
                result_text = call_claude(
                    prompt, model=model, timeout=_APPLICABILITY_TIMEOUT, max_turns=1
                )
            except RuntimeError:
                continue  # 跳过失败样本,用剩余
            sample = self._parse_applicability(result_text, n)
            for i, applicable in enumerate(sample):
                if applicable:
                    votes[i] += 1
            samples += 1

        if samples == 0:
            # LLM 全不可用:保守保留全部(不误剔,退到关键词法现状)
            return [True] * n
        min_needed = (_APPLICABILITY_VOTES + 1) // 2  # ceil(半数)
        if samples < min_needed:
            # 成功样本太薄(< 半数请求):不据此剔,保守全保留
            return [True] * n
        threshold = samples / 2  # >= → 保留(平票保守保留)
        return [votes[i] >= threshold for i in range(n)]

    @staticmethod
    def _parse_applicability(text: str, n: int) -> list[bool]:
        """解析 LLM 适用性判定;默认保留(保守,不误剔)。"""
        mask = [True] * n
        arr_str = _extract_first_balanced(text)
        if arr_str is None:
            return mask
        try:
            items = json.loads(arr_str)
        except json.JSONDecodeError:
            return mask
        if not isinstance(items, list):
            return mask
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                mask[idx] = bool(item.get("applicable", True))
        return mask

    def _check_instruction(
        self,
        instruction: InstructionCheck,
        transcript: str,
        output_dir: Path,
        model: str | None,
        timeout: int,
        tool_trace: list | None = None,
    ) -> ComplianceResultItem:
        if instruction.check_method == CheckMethod.TOOL_CALL_CHECK:
            return self._check_tool_call(instruction, transcript, tool_trace)
        elif instruction.check_method in (CheckMethod.PATTERN_MATCH, CheckMethod.FILE_PRESENCE):
            return self._check_programmatic(instruction, transcript, output_dir)
        else:
            return self._check_llm_single(instruction, transcript, model, _SINGLE_TIMEOUT)

    def _check_tool_call(self, instruction: InstructionCheck, transcript: str, tool_trace: list | None = None) -> ComplianceResultItem:
        tool_name = instruction.params.get("tool_name", "")
        script_path = instruction.params.get("script_path", "")
        lower = transcript.lower()

        if tool_trace:
            for tc in tool_trace:
                tc_name = tc.name if hasattr(tc, "name") else tc.get("name", "")
                tc_input = tc.input_summary if hasattr(tc, "input_summary") else tc.get("input_summary", "")
                if tool_name and tool_name.lower() in tc_name.lower():
                    return ComplianceResultItem(
                        instruction=instruction,
                        followed=True,
                        evidence=f"Tool '{tc_name}' found in tool trace with input: {tc_input[:100]}",
                        confidence=1.0,
                        source="programmatic",
                    )
                if script_path and script_path.lower() in tc_input.lower():
                    return ComplianceResultItem(
                        instruction=instruction,
                        followed=True,
                        evidence=f"Script path '{script_path}' found in tool call '{tc_name}': {tc_input[:100]}",
                        confidence=1.0,
                        source="programmatic",
                    )

        tool_used = tool_name.lower() in lower if tool_name else False
        script_found = script_path.lower() in lower if script_path else False

        return ComplianceResultItem(
            instruction=instruction,
            followed=tool_used or script_found,
            evidence=f"Tool '{tool_name}' {'found' if tool_used else 'not found'} in transcript"
                      + (f", script '{script_path}' {'found' if script_found else 'not found'}" if script_path else ""),
            confidence=1.0 if tool_trace else 0.7,
            source="programmatic",
        )

    def _check_programmatic(
        self, instruction: InstructionCheck, transcript: str, output_dir: Path
    ) -> ComplianceResultItem:
        if instruction.check_method == CheckMethod.FILE_PRESENCE:
            path = instruction.params.get("path", "")
            target = output_dir / path
            exists = target.exists()
            return ComplianceResultItem(
                instruction=instruction,
                followed=exists,
                evidence=f"File {path} {'exists' if exists else 'not found'}",
                confidence=1.0,
                source="programmatic",
            )

        sample = instruction.params.get("sample", "")
        if sample:
            match = any(line.strip() in transcript for line in sample.split("\n") if line.strip())
            return ComplianceResultItem(
                instruction=instruction,
                followed=match,
                evidence="Pattern match in transcript" if match else "Pattern not found",
                confidence=0.7 if match else 0.3,
                source="programmatic",
            )

        return ComplianceResultItem(
            instruction=instruction,
            followed=False,
            evidence="No programmatic check available",
            confidence=0.0,
            source="programmatic",
        )

    def _check_llm_batch(
        self,
        instructions: list[InstructionCheck],
        transcript: str,
        model: str | None,
        timeout: int,
    ) -> list[ComplianceResultItem]:
        instruction_list = ""
        for i, inst in enumerate(instructions):
            instruction_list += (
                f"[{i}] Section: {inst.section} | Priority: {inst.priority}\n"
                f"    {inst.instruction_text}\n\n"
            )

        prompt = (
            f"You are a compliance checker. Determine if each instruction was followed during execution.\n\n"
            f"## Instructions to check ({len(instructions)} total)\n\n"
            f"{instruction_list}\n"
            f"## Transcript excerpt\n\n{transcript[:8000]}\n\n"
            f"## Output Format\n\n"
            f"Respond with ONLY a JSON array. Each element:\n"
            f'{{"index": 0, "followed": true/false, "evidence": "brief explanation", "confidence": 0.0-1.0}}\n\n'
            f"Judge each instruction independently. Be strict: if no clear evidence, mark followed=false.\n"
            f"All evidence strings must be in Chinese (中文)."
        )

        try:
            result_text = call_claude(prompt, model=model, timeout=timeout, max_turns=1)
            return self._parse_batch_result(result_text, instructions)
        except RuntimeError:
            return [
                ComplianceResultItem(
                    instruction=inst,
                    followed=False,
                    evidence="LLM 批量检查不可用",
                    confidence=0.0,
                    source="llm",
                )
                for inst in instructions
            ]

    @staticmethod
    def _parse_batch_result(
        text: str, instructions: list[InstructionCheck]
    ) -> list[ComplianceResultItem]:
        defaults = [
            ComplianceResultItem(
                instruction=inst,
                followed=False,
                evidence="LLM 未评估此指令",
                confidence=0.0,
                source="llm",
            )
            for inst in instructions
        ]

        array_str = _extract_first_balanced(text)
        if array_str is None:
            return defaults

        try:
            items = json.loads(array_str)
        except json.JSONDecodeError:
            return defaults

        if not isinstance(items, list):
            return defaults

        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if not isinstance(idx, int) or idx < 0 or idx >= len(defaults):
                continue
            defaults[idx] = ComplianceResultItem(
                instruction=instructions[idx],
                followed=bool(item.get("followed", False)),
                evidence=str(item.get("evidence", "")),
                confidence=float(item.get("confidence", 0.5)),
                source="llm",
            )

        return defaults

    def _check_llm_single(
        self,
        instruction: InstructionCheck,
        transcript: str,
        model: str | None,
        timeout: int,
    ) -> ComplianceResultItem:
        prompt = (
            f"You are a compliance checker. Determine if the following instruction was followed "
            f"during execution.\n\n"
            f"Instruction: {instruction.instruction_text}\n"
            f"Section: {instruction.section}\n"
            f"Priority: {instruction.priority}\n\n"
            f"Transcript excerpt:\n{transcript[:4000]}\n\n"
            f"Respond with JSON: {{\"followed\": true/false, \"evidence\": \"explanation\", \"confidence\": 0.0-1.0}}\n"
            f"All evidence strings must be in Chinese (中文)."
        )

        try:
            result_text = call_claude(prompt, model=model, timeout=timeout, max_turns=1)
            obj_str = _extract_first_balanced(result_text, "{", "}")
            if obj_str:
                data = json.loads(obj_str)
                return ComplianceResultItem(
                    instruction=instruction,
                    followed=bool(data.get("followed", False)),
                    evidence=str(data.get("evidence", "")),
                    confidence=float(data.get("confidence", 0.5)),
                    source="llm",
                )
        except Exception:
            pass

        return ComplianceResultItem(
            instruction=instruction,
            followed=False,
            evidence="LLM 检查不可用",
            confidence=0.0,
            source="llm",
        )
