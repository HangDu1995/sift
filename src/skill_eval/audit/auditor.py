"""auditor: transcript 对账的核心判定。

``TranscriptAuditor`` 把 skill 声明的指令逐条对照 transcript 的实测行为,
产出 ``AuditFinding``(三态:PASS / FAIL / N/A)。

本模块 v1 仅实现**条件性指令判定**(``_judge_conditional``):
- TOOL_REQUIREMENT / REFERENCE_USAGE(params 含 tool_name 或 script_path)
- 命中 tool_trace → PASS(确定性程序判定,**不调 LLM**);
- 未命中 → 批量交 LLM 判"场景是否该触发该指令":
  - applicable=true  → FAIL(该做没做)
  - applicable=false → N/A(场景不涉及)
  - LLM 失败/异常/解析失败/批量超限 → **保守 FAIL**(不误判 N/A 放过疑似违规)

设计要点(对照 compliance.checker):
- 复用 ``match_tool_call`` / ``extract_first_balanced``(Task 3 抽出的公共件),不重写。
- 场景触发判定的 prompt + JSON 解析**仿** ``_llm_judge_constraint_applicability`` /
  ``_parse_applicability``,但语义不同(compliance 判"指令可不可遵循",audit 判
  "场景该不该触发"),且**默认值相反**:compliance 解析失败默认保留(True,不误剔),
  audit 解析失败默认保守 FAIL(None,不误判 N/A)。

禁令/步骤/OUTPUT 判定留后续 task;聚合/路由留后续 task。
"""
from __future__ import annotations

import json

from skill_eval.compliance.checker import extract_first_balanced, match_tool_call
from skill_eval.models import InstructionCheck
from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditVerdict,
)
from skill_eval.utils import call_claude

_MAX_LLM_IN_BATCH = 30  # 与 compliance.checker._MAX_LLM_IN_BATCH 对齐
# 保守 FAIL 的置信度(LLM 判定失败 / 批量超限):低,表明不确定而非确证违规。
_CONSERVATIVE_FAIL_CONFIDENCE = 0.3
# LLM 给出明确 applicable 时的置信度(该做没做 / 场景不涉及 都是明确结论)。
_RESOLVED_CONFIDENCE = 0.8
# 命中 tool_trace 是确定性程序判定,置信度满分。
_MATCH_CONFIDENCE = 1.0
# 防止场景描述过长撑爆 prompt(与 compliance 批量检查的 8000 截断一致)。
_SCENE_TRUNCATE = 8000


class TranscriptAuditor:
    """transcript 对账器:指令 vs 实测行为 → AuditFinding。

    v1 仅含 ``_judge_conditional``;聚合/路由等公共入口留 Task 8。
    """

    def _judge_conditional(
        self,
        instructions: list[InstructionCheck],
        tool_trace: list,
        transcript_scene: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 120,
    ) -> list[AuditFinding]:
        """判定条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态结论。

        每条指令先走确定性 ``match_tool_call``(命中 tool_trace → PASS,不调 LLM);
        未命中的批量交 LLM 判"场景是否该触发该指令",据此分 FAIL(该做没做) /
        N/A(场景不涉及);LLM 失败/异常/解析失败/批量超限 → 保守 FAIL。

        Args:
            instructions: 已筛出的条件性指令(params 含 tool_name 或 script_path)。
            tool_trace: ToolCallRecord 列表(支持 attr / dict 风格,见 match_tool_call)。
            transcript_scene: 场景描述(调用方从 digest.user_turns 拼接传入)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 CONDITIONAL;
            source = 指令的 source 值;derived_from_transcript 暂默认 False
            (Task 8 聚合时按是否来自反推声明再标)。
        """
        # 按原位置填,保证返回顺序与输入一致。
        findings: list[AuditFinding | None] = [None] * len(instructions)
        # 未命中的待 LLM 判定:(原位置 i, 指令)
        unmatched: list[tuple[int, InstructionCheck]] = []

        for i, inst in enumerate(instructions):
            tool_name = inst.params.get("tool_name", "")
            script_path = inst.params.get("script_path", "")
            if match_tool_call(tool_name, script_path, tool_trace):
                findings[i] = self._make_match_pass_finding(
                    inst, tool_name, script_path, transcript_name
                )
            else:
                unmatched.append((i, inst))

        if not unmatched:
            return [f for f in findings if f is not None]

        batch = unmatched[:_MAX_LLM_IN_BATCH]
        overflow = unmatched[_MAX_LLM_IN_BATCH:]

        applicable_mask = self._llm_judge_applicable(
            [inst for _, inst in batch], transcript_scene, model, timeout
        )

        for local_pos, (orig_i, inst) in enumerate(batch):
            tool_name = inst.params.get("tool_name", "")
            script_path = inst.params.get("script_path", "")
            miss_desc = f"调用 {tool_name}" if tool_name else f"读取 {script_path}"
            applicable = applicable_mask[local_pos]
            if applicable is True:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    category=AuditCategory.CONDITIONAL,
                    evidence=f"场景应触发但 trace 未{miss_desc}",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            elif applicable is False:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    category=AuditCategory.CONDITIONAL,
                    evidence="场景不涉及该指令",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/该条未被提及)→ 保守 FAIL
                findings[orig_i] = self._make_conservative_fail_finding(
                    inst, transcript_name, "LLM 判定失败,保守判疑似违规"
                )

        for orig_i, inst in overflow:
            findings[orig_i] = self._make_conservative_fail_finding(
                inst, transcript_name, "批量超限未判"
            )

        return [f for f in findings if f is not None]

    # ---- finding 工厂 -------------------------------------------------------

    @staticmethod
    def _make_match_pass_finding(
        inst: InstructionCheck,
        tool_name: str,
        script_path: str,
        transcript_name: str,
    ) -> AuditFinding:
        """命中 tool_trace 的 PASS finding。evidence 按 params 里设的键选措辞。"""
        if tool_name:
            evidence = f"trace 中调用了 {tool_name}"
        else:
            evidence = f"trace 中读取了 {script_path}"
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source=inst.source.value,
            verdict=AuditVerdict.PASS,
            category=AuditCategory.CONDITIONAL,
            evidence=evidence,
            confidence=_MATCH_CONFIDENCE,
            transcript=transcript_name,
        )

    @staticmethod
    def _make_conservative_fail_finding(
        inst: InstructionCheck, transcript_name: str, evidence: str
    ) -> AuditFinding:
        """保守 FAIL(LLM 失败 / 批量超限):低置信度,不误判 N/A 放过疑似违规。"""
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source=inst.source.value,
            verdict=AuditVerdict.FAIL,
            category=AuditCategory.CONDITIONAL,
            evidence=evidence,
            confidence=_CONSERVATIVE_FAIL_CONFIDENCE,
            transcript=transcript_name,
        )

    # ---- LLM 场景触发判定 ----------------------------------------------------

    def _llm_judge_applicable(
        self,
        candidates: list[InstructionCheck],
        transcript_scene: str,
        model: str | None,
        timeout: int,
    ) -> list[bool | None]:
        """单次 call_claude 判每条未命中指令在该场景是否"该触发"。

        返回与 candidates 等长的列表:
        - True  = 该触发(→ FAIL,该做没做)
        - False = 不涉及(→ N/A,场景不触发)
        - None  = 判定失败(→ 保守 FAIL)

        调用异常(RuntimeError,如 CLI 挂/超时)→ 全 None(全保守 FAIL)。
        """
        n = len(candidates)
        instruction_list = "".join(
            f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(candidates)
        )
        prompt = (
            "判断以下每条条件性指令在给定场景下是否应该被触发执行。\n\n"
            "## 判定标准\n"
            "- ✅ true(该触发):这个场景满足指令的隐含条件——执行者本应做该动作"
            "(如场景涉及该工具应被调用、该参考文件应被读取),却没做 = 疑似违规。\n"
            "- ❌ false(不涉及):这个场景根本不涉及该指令(场景未触及该工具 / 参考"
            "文件的适用范围,本就不该触发)。\n\n"
            f"## 场景描述\n{transcript_scene[:_SCENE_TRUNCATE]}\n\n"
            f"## 待判指令 ({n} 条)\n{instruction_list}\n"
            "## 输出格式\n"
            '只返回 JSON 数组,每元素:{"index": 0, "applicable": true/false}\n'
        )
        try:
            result_text = call_claude(
                prompt, model=model, timeout=timeout, max_turns=1
            )
        except RuntimeError:
            return [None] * n  # LLM 不可用:全保守 FAIL(不误判 N/A 放过疑似违规)
        return self._parse_applicable(result_text, n)

    @staticmethod
    def _parse_applicable(text: str, n: int) -> list[bool | None]:
        """解析 LLM 场景触发判定;默认保守 FAIL(None)。

        与 compliance.checker._parse_applicability 同构(同 JSON 格式 +
        extract_first_balanced),但默认值相反:compliance 默认 True(不误剔可遵循项),
        audit 默认 None(保守 FAIL,不误判 N/A 放过疑似违规)。LLM 未提及的条目、以及
        带了 index 但 applicable 缺键/None/非布尔(如字符串 "false")的畸形条目,
        一律保持 None → 保守 FAIL(严格 is True/is False 判断,不用 bool() 把畸形值洗成 N/A)。
        """
        mask: list[bool | None] = [None] * n
        arr_str = extract_first_balanced(text)
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
                val = item.get("applicable")
                if val is True:
                    mask[idx] = True
                elif val is False:
                    mask[idx] = False
                # else: 缺 applicable 键 / None / 非布尔(如字符串 "false")
                # → 保持 None → 保守 FAIL(不误判 N/A 放过疑似违规)
        return mask
