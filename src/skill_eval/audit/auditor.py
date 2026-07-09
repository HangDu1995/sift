"""auditor: transcript 对账的核心判定。

``TranscriptAuditor`` 把 skill 声明的指令逐条对照 transcript 的实测行为,
产出 ``AuditFinding``(三态:PASS / FAIL / N/A)。按指令类别分四条判定路径:

- **条件性**(``_judge_conditional``):TOOL_REQUIREMENT / REFERENCE_USAGE
  - 命中 tool_trace → PASS(确定性程序判定,**不调 LLM**);
  - 未命中 → 批量交 LLM 判"场景是否该触发该指令":
    - applicable=true  → FAIL(该做没做)
    - applicable=false → N/A(场景不涉及)
    - LLM 失败/异常/解析失败/批量超限 → **保守 FAIL**(不误判 N/A 放过疑似违规)
- **禁令**(``_judge_prohibition``):CONSTRAINT / ANTI_FABRICATION
  - 批量交 LLM **主动检测违规是否发生**(不是判 followed):
    - violated=true  → FAIL(违规了)
    - violated=false → PASS(没违规)
    - LLM 失败/异常/解析失败/批量超限/畸形值 → **保守 FAIL**(禁令违规严重,宁可标记让人工核实)
- **步骤**(``_judge_step``):HEADING_STEP
  - 批量交 LLM 判三态:pass→PASS / fail→FAIL / na→N/A;未知值/异常 → 保守 FAIL
- **输出格式**(``_judge_output``):OUTPUT_FORMAT(PATTERN_MATCH)
  - 确定性子串判定(**不调 LLM**):sample 任一行(strip 后)在 transcript 命中 → PASS,否则 FAIL
- ``classify`` 把 ``InstructionSource`` 路由到对应 ``AuditCategory``,供 Task 8 聚合主入口用。

设计要点(对照 compliance.checker):
- 复用 ``match_tool_call`` / ``extract_first_balanced``(Task 3 抽出的公共件),不重写。
- OUTPUT 的子串判定**照** ``_check_programmatic`` 的 PATTERN_MATCH 分支逻辑。
- LLM 批量 prompt + JSON 解析**仿** ``_check_llm_batch`` / ``_parse_applicability``,
  但 audit 的语义是"实测里有没有发生某行为",且**默认值保守 FAIL**(compliance 解析失败
  默认保留 True 不误剔;audit 解析失败默认保守 FAIL 不误判 N/A / PASS 放过疑似违规)。

聚合/路由主入口 ``audit``:按 ``classify`` 把指令分到上述四法、标 derived、汇总 summary。
"""
from __future__ import annotations

import json
from pathlib import Path

from skill_eval.audit.declare import derive_declarations
from skill_eval.audit.rebuild import rebuild
from skill_eval.compliance.checker import extract_first_balanced, match_tool_call
from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.errors import SkillEvalError
from skill_eval.models import InstructionCheck, InstructionSource
from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditReport,
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

    含四条按类别分的判定路径(``_judge_conditional`` / ``_judge_prohibition`` /
    ``_judge_step`` / ``_judge_output``)+ ``classify`` 类别路由;
    ``audit`` 是聚合/路由主入口(串联上述件 + InstructionExtractor + rebuild + derive)。
    """

    @staticmethod
    def classify(instruction: InstructionCheck) -> AuditCategory:
        """把指令的 ``InstructionSource`` 路由到对应的 ``AuditCategory``。

        - TOOL_REQUIREMENT / REFERENCE_USAGE → CONDITIONAL(条件性,有触发条件)
        - CONSTRAINT / ANTI_FABRICATION      → PROHIBITION(禁令类)
        - HEADING_STEP                       → STEP(步骤)
        - OUTPUT_FORMAT                      → OUTPUT(输出格式)

        供 Task 8 聚合主入口按类别分流到各 ``_judge_*``。未映射的 source 视为编程错误
        (``InstructionSource`` 扩展后须同步更新本路由)→ 抛 ValueError 以便早暴露。
        """
        source = instruction.source
        if source in (InstructionSource.TOOL_REQUIREMENT, InstructionSource.REFERENCE_USAGE):
            return AuditCategory.CONDITIONAL
        if source in (InstructionSource.CONSTRAINT, InstructionSource.ANTI_FABRICATION):
            return AuditCategory.PROHIBITION
        if source == InstructionSource.HEADING_STEP:
            return AuditCategory.STEP
        if source == InstructionSource.OUTPUT_FORMAT:
            return AuditCategory.OUTPUT
        raise ValueError(f"未映射的 InstructionSource: {source!r}")

    def audit(
        self,
        skill_name: str,
        skill_body: str,
        transcripts: list[Path],
        model: str | None = None,
        timeout: int = 120,
    ) -> AuditReport:
        """聚合主入口:把 skill 声明的指令 + transcript 反推声明逐条对账若干 transcript。

        把 Task 1-7 的零件串起来。流程(每条 transcript 独立处理,findings 平铺
        per-transcript,顺序与 transcripts 入参一致):

        1. ``InstructionExtractor`` 从 skill_body 抽静态指令;
        2. 每 transcript ``rebuild`` 重建(tool_trace / text / digest)——坏/空 transcript
           抛 ``SkillEvalError`` → 进 warnings、跳过该条(不崩,其余继续);
        3. ``derive_declarations`` 从 digest 反推"真实使用里确实需要的"补充指令(只增不减,
           纯新增、去重 vs 静态指令);existing + derived 合并后按 ``classify`` 分四桶
           (CONDITIONAL / PROHIBITION / STEP / OUTPUT);
        4. 四个 ``_judge_*`` 各判各桶 → findings;derived 来的 finding 标
           ``derived_from_transcript=True``(按 instruction_text 命中 derived_texts 集合);
        5. 汇总 summary(total/pass/fail/na)+ warnings → ``AuditReport``。

        Args:
            skill_name: skill 名(填进 report.skill_name)。
            skill_body: SKILL.md 正文(``InstructionExtractor.extract`` 的输入)。
            transcripts: Claude Code session JSONL 路径列表(逐个 rebuild)。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            ``AuditReport``:findings 为所有**有效** transcript 的平铺结果;坏/空 transcript
            不进 findings,而是带文件名进 warnings。
        """
        instructions = InstructionExtractor().extract(skill_body)
        warnings: list[str] = []
        all_findings: list[AuditFinding] = []
        transcript_names: list[str] = []

        for tp in transcripts:
            try:
                tool_trace, transcript_text, digest = rebuild(tp)
            except SkillEvalError as e:
                # 坏/空 transcript:不崩,记 warning 跳过(其余 transcript 照常处理)。
                warnings.append(f"{tp.name}: {e}")
                continue

            derived = derive_declarations(digest, instructions)
            all_inst = instructions + derived
            derived_texts = {d.instruction_text for d in derived}

            buckets: dict[AuditCategory, list[InstructionCheck]] = {
                AuditCategory.CONDITIONAL: [],
                AuditCategory.PROHIBITION: [],
                AuditCategory.STEP: [],
                AuditCategory.OUTPUT: [],
            }
            for inst in all_inst:
                buckets[self.classify(inst)].append(inst)

            scene = "\n".join(digest.user_turns)

            findings: list[AuditFinding] = []
            findings += self._judge_conditional(
                buckets[AuditCategory.CONDITIONAL], tool_trace, scene, tp.name, model, timeout
            )
            findings += self._judge_prohibition(
                buckets[AuditCategory.PROHIBITION], transcript_text, tp.name, model, timeout
            )
            findings += self._judge_step(
                buckets[AuditCategory.STEP], transcript_text, tp.name, model, timeout
            )
            findings += self._judge_output(
                buckets[AuditCategory.OUTPUT], transcript_text, tp.name
            )

            for f in findings:
                if f.instruction_text in derived_texts:
                    f.derived_from_transcript = True

            all_findings.extend(findings)
            transcript_names.append(tp.name)

        summary = {
            "total": len(all_findings),
            "pass": sum(1 for f in all_findings if f.verdict == AuditVerdict.PASS),
            "fail": sum(1 for f in all_findings if f.verdict == AuditVerdict.FAIL),
            "na": sum(1 for f in all_findings if f.verdict == AuditVerdict.NA),
        }
        return AuditReport(
            skill_name=skill_name,
            transcripts=transcript_names,
            summary=summary,
            findings=all_findings,
            warnings=warnings,
        )

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

    # ---- 禁令类:主动检测违规 -------------------------------------------------

    def _judge_prohibition(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 120,
    ) -> list[AuditFinding]:
        """判定禁令类指令(CONSTRAINT / ANTI_FABRICATION)——**主动检测违规是否发生**。

        与条件性/步骤判"该不该做/做没做"不同,禁令类问"这次真实执行里有没有发生违反
        该禁令/约束的行为"(如编造数据、跳过必须步骤、用了被禁止的做法)。批量交 LLM,
        每条返回 ``{index, violated: bool, evidence}``:
        - violated=True  → FAIL(违规了)
        - violated=False → PASS(没违规)
        - LLM 异常 / 解析失败 / 未提及 / 批量超限 / violated 缺键或非布尔 → **保守 FAIL**
          (禁令违规严重,宁可标记让人工核实;严格 ``is True/is False`` 判断,畸形值不当 PASS)。

        Args:
            instructions: 已筛出的禁令类指令(CONSTRAINT / ANTI_FABRICATION)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 PROHIBITION。
        """
        findings: list[AuditFinding | None] = [None] * len(instructions)
        batch = instructions[:_MAX_LLM_IN_BATCH]
        overflow = instructions[_MAX_LLM_IN_BATCH:]

        violated_mask = self._llm_judge_prohibition(
            batch, transcript_text, model, timeout
        )

        for i, inst in enumerate(batch):
            violated, ev = violated_mask[i]
            if violated is True:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中发生了违反该禁令的行为",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            elif violated is False:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中未违反该禁令",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/未提及/畸形值)→ 保守 FAIL
                findings[i] = self._make_conservative_fail_finding(
                    inst, transcript_name, "LLM 判定失败,保守判疑似违规",
                    category=AuditCategory.PROHIBITION,
                )

        for offset, inst in enumerate(overflow):
            findings[_MAX_LLM_IN_BATCH + offset] = self._make_conservative_fail_finding(
                inst, transcript_name, "批量超限未判",
                category=AuditCategory.PROHIBITION,
            )

        return [f for f in findings if f is not None]

    # ---- 步骤类:三态判定 ----------------------------------------------------

    def _judge_step(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 120,
    ) -> list[AuditFinding]:
        """判定步骤类指令(HEADING_STEP)的三态结论。

        批量交 LLM,每条返回 ``{index, verdict: "pass"|"fail"|"na", evidence}``,
        判定标准="这次执行有没有走这一步:走了→pass;该走没走→fail;场景不涉及该步→na":
        - pass → PASS
        - fail → FAIL
        - na   → N/A
        - 未知 verdict / LLM 异常 / 解析失败 / 批量超限 → **保守 FAIL**(不误判 N/A 放过)。

        verdict 匹配大小写不敏感(strip+lower);未知值/非字符串 → 保守 FAIL。

        Args:
            instructions: 已筛出的步骤类指令(HEADING_STEP)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 STEP。
        """
        findings: list[AuditFinding | None] = [None] * len(instructions)
        batch = instructions[:_MAX_LLM_IN_BATCH]
        overflow = instructions[_MAX_LLM_IN_BATCH:]

        verdict_mask = self._llm_judge_step(batch, transcript_text, model, timeout)

        for i, inst in enumerate(batch):
            v, ev = verdict_mask[i]
            if v == "pass":
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    category=AuditCategory.STEP,
                    evidence=ev or "执行中走过了该步",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            elif v == "fail":
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    category=AuditCategory.STEP,
                    evidence=ev or "该走这一步但未走",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            elif v == "na":
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    category=AuditCategory.STEP,
                    evidence=ev or "场景不涉及该步",
                    confidence=_RESOLVED_CONFIDENCE,
                    transcript=transcript_name,
                )
            else:
                # None/未知值/非字符串 → 保守 FAIL(不误判 N/A 放过)
                findings[i] = self._make_conservative_fail_finding(
                    inst, transcript_name, "LLM 判定失败,保守判疑似违规",
                    category=AuditCategory.STEP,
                )

        for offset, inst in enumerate(overflow):
            findings[_MAX_LLM_IN_BATCH + offset] = self._make_conservative_fail_finding(
                inst, transcript_name, "批量超限未判",
                category=AuditCategory.STEP,
            )

        return [f for f in findings if f is not None]

    # ---- 输出格式:确定性子串判定 -------------------------------------------

    def _judge_output(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
    ) -> list[AuditFinding]:
        """判定 OUTPUT_FORMAT(PATTERN_MATCH)——确定性子串判定,**不调 LLM**。

        每条指令取 ``params.get("sample","")``,按行拆,任一行 strip 后作为子串在
        ``transcript_text`` 命中 → PASS,否则 FAIL(照 ``compliance.checker._check_programmatic``
        的 PATTERN_MATCH 分支逻辑)。confidence=1.0(确定性程序判定)。

        Args:
            instructions: 已筛出的 OUTPUT_FORMAT 指令(params 含 sample)。
            transcript_text: 该 transcript 的执行记录文本(完整传入,作子串匹配源)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 OUTPUT;confidence=1.0。
        """
        findings: list[AuditFinding] = []
        for inst in instructions:
            sample = inst.params.get("sample", "")
            # 照 checker PATTERN_MATCH:按行拆、strip、跳过空行、任一行作为子串命中即 True。
            lines = [ln.strip() for ln in sample.split("\n")]
            matched = next((ln for ln in lines if ln and ln in transcript_text), None)
            if matched is not None:
                findings.append(AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    category=AuditCategory.OUTPUT,
                    evidence=f"输出含期望格式 {matched[:80]}",
                    confidence=_MATCH_CONFIDENCE,
                    transcript=transcript_name,
                ))
            else:
                label = next((ln for ln in lines if ln), "")
                if label:
                    evidence = f"未找到期望格式 {label[:80]}"
                else:
                    evidence = "未提供期望输出格式样本"
                findings.append(AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    category=AuditCategory.OUTPUT,
                    evidence=evidence,
                    confidence=_MATCH_CONFIDENCE,
                    transcript=transcript_name,
                ))
        return findings

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
        inst: InstructionCheck,
        transcript_name: str,
        evidence: str,
        category: AuditCategory = AuditCategory.CONDITIONAL,
    ) -> AuditFinding:
        """保守 FAIL(LLM 失败 / 批量超限 / 解析失败):低置信度,不误判 N/A 放过疑似违规。

        ``category`` 默认 CONDITIONAL(条件性);prohibition / step 各自传入对应类别,
        以便 finding.category 反映真实指令类别而非恒为 conditional。
        """
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source=inst.source.value,
            verdict=AuditVerdict.FAIL,
            category=category,
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

    # ---- 禁令类 LLM 违规检测 -------------------------------------------------

    def _llm_judge_prohibition(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
    ) -> list[tuple[bool | None, str]]:
        """单次 call_claude 判每条禁令在本次执行里是否被违反。

        返回与 candidates 等长的列表,每元素 ``(violated, evidence)``:
        - (True, ev)  = 已违反(→ FAIL)
        - (False, ev) = 未违反(→ PASS)
        - (None, "")  = 判定失败/未提及/畸形值(→ 保守 FAIL)

        调用异常(RuntimeError,如 CLI 挂/超时)→ 全 (None, "")(全保守 FAIL)。
        """
        n = len(candidates)
        instruction_list = "".join(
            f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(candidates)
        )
        prompt = (
            "判断以下每条禁令/约束在本次真实执行里是否被违反。\n\n"
            "## 判定标准\n"
            "- ✅ violated=true(已违反):这次执行里确实发生了违反该禁令/约束的行为"
            "(如编造了数据、跳过了必须步骤、用了被禁止的做法)。\n"
            "- ❌ violated=false(未违反):这次执行没有违反该禁令/约束。\n\n"
            f"## 执行记录摘录\n{transcript_text[:_SCENE_TRUNCATE]}\n\n"
            f"## 待判禁令 ({n} 条)\n{instruction_list}\n"
            "## 输出格式\n"
            '只返回 JSON 数组,每元素:'
            '{"index": 0, "violated": true/false, "evidence": "中文简述为何违反/未违反"}\n'
        )
        try:
            result_text = call_claude(
                prompt, model=model, timeout=timeout, max_turns=1
            )
        except RuntimeError:
            return [(None, "")] * n  # LLM 不可用:全保守 FAIL(禁令违规严重,宁可标记让人工核实)
        return self._parse_prohibition(result_text, n)

    @staticmethod
    def _parse_prohibition(
        text: str, n: int
    ) -> list[tuple[bool | None, str]]:
        """解析 LLM 禁令违规判定;默认保守 FAIL((None, ""))。

        与 ``_parse_applicable`` 同构(同 JSON 格式 + extract_first_balanced + 严格
        ``is True/is False``),只是多取一个 evidence 字段。LLM 未提及的条目、以及带
        了 index 但 violated 缺键/None/非布尔(如字符串 "true")的畸形条目,一律保持
        (None, "") → 保守 FAIL(不用 bool() 把 "false" 洗成 True 误判违规)。
        """
        results: list[tuple[bool | None, str]] = [(None, "")] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return results
        try:
            items = json.loads(arr_str)
        except json.JSONDecodeError:
            return results
        if not isinstance(items, list):
            return results
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                violated = item.get("violated")
                ev = str(item.get("evidence", "") or "")
                if violated is True:
                    results[idx] = (True, ev)
                elif violated is False:
                    results[idx] = (False, ev)
                # else: 缺 violated 键 / None / 非布尔 → 保持 (None, "") → 保守 FAIL
        return results

    # ---- 步骤类 LLM 三态判定 -------------------------------------------------

    def _llm_judge_step(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
    ) -> list[tuple[str | None, str]]:
        """单次 call_claude 判每个步骤在本次执行里是否被走过。

        返回与 candidates 等长的列表,每元素 ``(verdict, evidence)``:
        - ("pass", ev) = 走过(→ PASS)
        - ("fail", ev) = 该走没走(→ FAIL)
        - ("na", ev)   = 场景不涉及(→ N/A)
        - (None, "")   = 判定失败/未提及/未知 verdict/非字符串(→ 保守 FAIL)

        调用异常(RuntimeError)→ 全 (None, "")(全保守 FAIL)。
        """
        n = len(candidates)
        instruction_list = "".join(
            f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(candidates)
        )
        prompt = (
            "判断以下每个步骤在本次执行里是否被走过。\n\n"
            "## 判定标准\n"
            "- pass:这次执行走过了这一步。\n"
            "- fail:这个场景本该走这一步但没走。\n"
            "- na:这个场景根本不涉及该步骤。\n\n"
            f"## 执行记录摘录\n{transcript_text[:_SCENE_TRUNCATE]}\n\n"
            f"## 待判步骤 ({n} 条)\n{instruction_list}\n"
            "## 输出格式\n"
            '只返回 JSON 数组,每元素:'
            '{"index": 0, "verdict": "pass"|"fail"|"na", "evidence": "中文简述"}\n'
        )
        try:
            result_text = call_claude(
                prompt, model=model, timeout=timeout, max_turns=1
            )
        except RuntimeError:
            return [(None, "")] * n  # LLM 不可用:全保守 FAIL
        return self._parse_step(result_text, n)

    @staticmethod
    def _parse_step(
        text: str, n: int
    ) -> list[tuple[str | None, str]]:
        """解析 LLM 步骤三态判定;默认保守 FAIL((None, ""))。

        verdict 匹配大小写不敏感(strip + lower);"pass"/"fail"/"na" 之外的未知值、
        非字符串、缺键 → 一律保持 (None, "") → 保守 FAIL(不误判 N/A 放过疑似漏步)。
        """
        results: list[tuple[str | None, str]] = [(None, "")] * n
        arr_str = extract_first_balanced(text)
        if arr_str is None:
            return results
        try:
            items = json.loads(arr_str)
        except json.JSONDecodeError:
            return results
        if not isinstance(items, list):
            return results
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            if isinstance(idx, int) and 0 <= idx < n:
                v = item.get("verdict")
                ev = str(item.get("evidence", "") or "")
                if isinstance(v, str):
                    vl = v.strip().lower()
                    if vl in ("pass", "fail", "na"):
                        results[idx] = (vl, ev)
                    # else: 未知 verdict 字符串 → 保持 (None, "") → 保守 FAIL
                # else: 缺 verdict 键 / 非字符串 → 保持 (None, "") → 保守 FAIL
        return results
