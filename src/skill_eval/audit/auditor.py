"""auditor: transcript 对账的核心判定。

``TranscriptAuditor`` 把 skill 声明的指令逐条对照 transcript 的实测行为,
产出 ``AuditFinding``(五态:PASS / FAIL / N/A / UNRESOLVED / INDETERMINATE)。每条 finding
另带 ``method``(PROGRAMMATIC / LLM,怎么查的)。按指令类别分四条判定路径:

- **条件性**(``_judge_conditional``):TOOL_REQUIREMENT / REFERENCE_USAGE
  - 命中 tool_trace → PASS(method=PROGRAMMATIC,**不调 LLM**);
  - 未命中 → 批量交 LLM 判"场景是否该触发该指令"(method=LLM):
    - applicable=true  → FAIL(该做没做)
    - applicable=false → N/A(场景不涉及)
    - indeterminate=true(带 reason) → INDETERMINATE(判不准)
    - LLM 失败/异常/解析失败/未提及/indeterminate 无 reason → **UNRESOLVED**(没判成)
- **禁令**(``_judge_prohibition``):CONSTRAINT / ANTI_FABRICATION
  - 批量交 LLM **主动检测违规是否发生**(method=LLM):
    - violated=true  → FAIL(违规了)
    - violated=false → PASS(没违规)
    - indeterminate=true(带 evidence) → INDETERMINATE(判不准)
    - LLM 失败/异常/解析失败/未提及/畸形值/indeterminate 无 evidence → **UNRESOLVED**(没判成)
- **步骤**(``_judge_step``):HEADING_STEP
  - 批量交 LLM 判(method=LLM):pass→PASS / fail→FAIL / na→N/A / indeterminate→INDETERMINATE;
    未知值/异常/indeterminate 无 evidence → UNRESOLVED(没判成)
  - 空桶(0 条)→ 短路返回 [],不调 LLM(同 prohibition)
- **输出格式**(``_judge_output``):OUTPUT_FORMAT(PATTERN_MATCH)
  - 确定性子串判定(method=PROGRAMMATIC,**不调 LLM**):sample 任一行(strip 后)在 transcript
    命中 → PASS,否则 FAIL
- ``classify`` 把 ``InstructionSource`` 路由到对应 ``AuditCategory``,供聚合主入口用。

**没判成 vs 判不准**(本层核心区分):
- UNRESOLVED(没判成)= 操作性失败:LLM 没出判定(调用/解析失败/未提及/畸形值),**可重跑**救回。
- INDETERMINATE(判不准)= 语义模糊:LLM 看了证据、明确说"证据相互矛盾/缺失、判不了",
  **重跑无益**(只能人工复核),且强制带 indeterminate_reason(写明具体矛盾/缺失)。

设计要点(对照已删的 compliance.checker 旧实现,helper 现归 audit.tool_match):
- 复用 ``match_tool_call`` / ``extract_first_balanced``,不重写。
- OUTPUT 的子串判定照 ``_check_programmatic`` 的 PATTERN_MATCH 分支逻辑。
- LLM 批量 prompt + JSON 解析仿 ``_check_llm_batch`` / ``_parse_applicability``。
- **FAIL 必须站得住**(宁可少报不能乱报):只有 LLM 明确判"违规 / 该做没做"才报 FAIL;
  LLM 不可用 / 解析失败 / 畸形值 / 未提及 → **UNRESOLVED**(没判成),``audit()`` 据此汇总
  "建议重跑"警告。与 compliance 解析失败默认保留(不误剔可遵循项)方向不同:
  compliance 偏不漏(默认遵循),audit 偏不冤(LLM 判准才报 FAIL)。
- **判不准三道滥用闸**:① parser 强制 indeterminate 必须带具体 reason/evidence,空则降级 UNRESOLVED;
  ② audit() 统计 indeterminate 占比,超阈值告警;③ prompt 写明纪律(优先明确结论,
  indeterminate 须写具体矛盾证据,不许偷懒)。

聚合/路由主入口 ``audit``:按 ``classify`` 把指令分到上述四法、标 derived、汇总 summary。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from skill_eval.audit.declare import derive_declarations
from skill_eval.audit.rebuild import rebuild
from skill_eval.audit.tool_match import extract_first_balanced, match_tool_call
from skill_eval.audit.aggregate import aggregate_by_instruction
from skill_eval.audit.dedup import post_process_findings
from skill_eval.compliance.extractor import InstructionExtractor
from skill_eval.errors import SkillEvalError
from skill_eval.evals.opencode import transcript_source
from skill_eval.models import InstructionCheck, InstructionSource
from skill_eval.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditReport,
    AuditVerdict,
)
from skill_eval.utils import call_claude

# 每批交 LLM 判定的指令数上限。**小于**已删 compliance.checker 的 30:audit 每条要 LLM 产出
# "verdict + 中文 evidence"(输出量大),且 LLM 延迟方差极大(实测 ~10 条/批 40-257s)。
# 30 条均值就 ~210s、慢尾更甚;10 条均值 ~117s、慢尾 ~257s,配 timeout=600 留足裕量
# (慢尾 257s + ~340s 裕量,几乎不超时)。批数虽多但每批绝对时长低,远比 30 条稳。
_MAX_LLM_IN_BATCH = 10
# 滥用闸 ②:判不准(indeterminate)占比超此阈值、且总条数 >= _MIN → audit() 告警。
# INDETERMINATE 是 LLM 的逃逸口(看不懂就能说"判不准"蒙混),需监控是否被滥用。
# 阈值宽松(20%)+ 最小样本(5):真模糊偶尔会有,只在显著滥用时告警,免误报。
_INDETERMINATE_ABUSE_RATE = 0.20
_INDETERMINATE_ABUSE_MIN_TOTAL = 5
# 防止场景描述过长撑爆 prompt(与 compliance 批量检查的 8000 截断一致)。
_SCENE_TRUNCATE = 8000
# 整批 LLM 判定 transient 失败(超时 / CLI 非零退出 / 限流)的重试次数与退避间隔。
# call_claude 在超时 / 非零退出时抛 RuntimeError,多为 transient——重试大多能救回整批,
# 避免单次抖动让一整批指令兜底 N/A(audit-out3 实测 2 个整批共 25 条因此放弃)。
# 重试只针对 RuntimeError(语义错误重试无益,且 call_claude 也只抛这一种)。
_LLM_RETRY_MAX = 2
_LLM_RETRY_DELAY = 3.0
# 三个 LLM 判定器(条件性/禁令/步骤)并发上限。= 类别数 = 实测安全并发(1/2/3 档全 0 失败),
# 既是全类别并发的最优值、又天然有界(再高也无更多类别可并发)。非魔法数、无需调参。
_MAX_CONCURRENT = 3

# 进度回调类型:audit 每批 LLM 判定 40-257s,全程静默对用户极不友好。核心只 emit 字符串,
# 由 CLI 层(传 console.print)/测试层(传 list.append)决定如何呈现,producer/consumer 分离。
ProgressFn = Callable[[str], None]


def _emit(on_progress: ProgressFn | None, msg: str) -> None:
    """None 安全的进度回调:on_progress 为 None 时 no-op(核心默认不耦合 I/O)。"""
    if on_progress is not None:
        on_progress(msg)


# 重试耗尽标记:_judge_batch_with_retry 返回它时,调用方跳过写入、保留预置的全 N/A 兜底。
_RETRY_EXHAUSTED = object()


def _judge_batch_with_retry(
    prompt: str,
    model: str | None,
    timeout: int,
    parse_fn: Callable[[str, int], list],
    bn: int,
    *,
    on_progress: ProgressFn | None,
    label: str,
    k: int,
    total: int,
    is_unparsed: Callable[[object], bool],
) -> object:
    """call+parse 一个 LLM 批,RuntimeError 或整批 0 解析成功则重试(共用 _LLM_RETRY_MAX 预算)。

    合并两种"这批没拿到可用判定"的失败,共用同一份重试预算(不再为 0-解析另设次数):

    - **调用失败**:call_claude 抛 RuntimeError(超时 / CLI 非零退出 / 限流)。
    - **解析为空**:调用成功但 parse_fn 全 None(LLM 输出畸形 / 截断 / 空数组)——原是静默
      盲点(打"完成"却全 N/A),现重试。实锤 0-解析多为瞬态:同 prompt 重试常能解出。

    重试上限 _LLM_RETRY_MAX,线性退避(_LLM_RETRY_DELAY × 尝试序号)。成功返回 parse_fn
    结果;重试耗尽返回 _RETRY_EXHAUSTED(调用方保留预置全 N/A 兜底,audit 汇总"未判定"警告)。
    """
    # 进度前缀:类别名前置、括号化(三类并发交错时一眼分清哪条是哪类);单批不显 [k/总]
    # (恒 [1/1] 是噪声、看着像坏掉的计数器),多批才带 "批 k/total"。
    batch_tag = f" 批 {k}/{total}" if total > 1 else ""
    prefix = f"[{label}]{batch_tag}"
    for attempt in range(_LLM_RETRY_MAX + 1):
        if attempt > 0:
            time.sleep(_LLM_RETRY_DELAY * attempt)
            _emit(on_progress, f"  {prefix} 重试 {attempt}/{_LLM_RETRY_MAX}…")
        _emit(on_progress, f"  {prefix} 判定 {bn} 条…")
        t0 = time.monotonic()
        try:
            result_text = call_claude(prompt, model=model, timeout=timeout, max_turns=1)
        except RuntimeError as exc:
            _emit(on_progress, f"  {prefix} 调用失败: {str(exc)[:120]}")
            continue
        _emit(on_progress, f"  {prefix} 完成 ⏱ {time.monotonic() - t0:.0f}s")
        parsed = parse_fn(result_text, bn)
        if bn and all(is_unparsed(v) for v in parsed):
            _emit(on_progress, f"  {prefix} ⚠ 0 条解析成功(LLM 输出畸形/截断?),重试…")
            continue
        return parsed
    _emit(on_progress, f"  {prefix} 重试耗尽 → 本批 {bn} 条全兜底 N/A")
    return _RETRY_EXHAUSTED


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
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
        refine_instructions: bool = False,
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
        if refine_instructions:
            instructions, refine_warns = self._refine_pseudo_instructions(
                instructions, model=model, timeout=timeout, on_progress=on_progress
            )
            warnings.extend(refine_warns)
        all_findings: list[AuditFinding] = []
        transcript_names: list[str] = []
        transcript_sources: list[str] = []  # 与 transcript_names 同序;"cc"/"opencode"/...

        for ti, tp in enumerate(transcripts, 1):
            _emit(on_progress, f"对账 transcript {ti}/{len(transcripts)}: {tp.name}")
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

            # 三个 LLM 判定器(条件性/禁令/步骤)互相独立、无共享可变状态,并发跑省墙钟
            # (实测 zh:串行 13+51+37=101s → 并发 ≈ max=51s,约 2×)。max_workers=3 =
            # 类别数 = 实测安全并发(3 档全 0 失败),天然有界、非魔法数。输出格式判定器
            # 是确定性子串匹配(无 LLM),留主线程不占池位。on_progress(console.print)
            # 线程安全,三类 [k/总] 交错可读。
            with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT) as pool:
                fut_cond = pool.submit(
                    self._judge_conditional, buckets[AuditCategory.CONDITIONAL],
                    tool_trace, scene, tp.name, model, timeout, on_progress=on_progress,
                )
                fut_proh = pool.submit(
                    self._judge_prohibition, buckets[AuditCategory.PROHIBITION],
                    transcript_text, tp.name, model, timeout, on_progress=on_progress,
                )
                fut_step = pool.submit(
                    self._judge_step, buckets[AuditCategory.STEP], transcript_text,
                    tp.name, model, timeout, tool_trace=tool_trace, on_progress=on_progress,
                )
                findings: list[AuditFinding] = []
                findings += fut_cond.result()
                findings += fut_proh.result()
                findings += fut_step.result()
            findings += self._judge_output(
                buckets[AuditCategory.OUTPUT], transcript_text, tp.name
            )

            for f in findings:
                if f.instruction_text in derived_texts:
                    f.derived_from_transcript = True

            all_findings.extend(findings)
            transcript_names.append(tp.name)
            transcript_sources.append(transcript_source(tp))

        # findings 后处理:Case A 同文去重(合并 sources)+ 编 #1..#N + Case B 跨类关联
        # (related 引 #N)。在汇总前做,使 unverified/summary/by_instruction 都基于去重后
        # findings(同文重复不再虚增 fail、by_instruction 不再同 transcript 双计)。
        all_findings = post_process_findings(all_findings)

        # 没判成(UNRESOLVED)vs 判不准(INDETERMINATE)分开汇总、不同警告:
        # - 没判成是操作性失败(LLM 调用/解析失败/未提及),**可重跑**救回 → 提示重跑。
        # - 判不准是语义模糊(LLM 看了证据真判不了),重跑无益 → 不提示重跑;
        #   但它是 LLM 的逃逸口,占比偏高 → 滥用闸 ② 告警(可能 LLM 偷懒说"判不准"蒙混)。
        # N/A(不适用)是 LLM 明确判"场景不涉及",属已解决结论,不进任何警告。
        unresolved = sum(1 for f in all_findings if f.verdict == AuditVerdict.UNRESOLVED)
        indeterminate = sum(1 for f in all_findings if f.verdict == AuditVerdict.INDETERMINATE)
        total = len(all_findings)
        if unresolved:
            warnings.append(
                f"{unresolved} 条指令没判成(UNRESOLVED:LLM 调用/解析失败/未提及)"
                "(本次结果可能不完整,建议重跑)"
            )
        if total >= _INDETERMINATE_ABUSE_MIN_TOTAL and indeterminate / total > _INDETERMINATE_ABUSE_RATE:
            warnings.append(
                f"{indeterminate} 条指令判不准(INDETERMINATE,占 {indeterminate * 100 // total}%),"
                "占比偏高,请人工复核是否 LLM 滥用判不准逃逸口"
            )

        summary = {
            "total": total,
            "pass": sum(1 for f in all_findings if f.verdict == AuditVerdict.PASS),
            "fail": sum(1 for f in all_findings if f.verdict == AuditVerdict.FAIL),
            "na": sum(1 for f in all_findings if f.verdict == AuditVerdict.NA),
            "unresolved": unresolved,
            "indeterminate": indeterminate,
        }
        return AuditReport(
            skill_name=skill_name,
            transcripts=transcript_names,
            transcript_sources=transcript_sources,
            summary=summary,
            findings=all_findings,
            by_instruction=aggregate_by_instruction(all_findings),
            warnings=warnings,
        )

    def _judge_conditional(
        self,
        instructions: list[InstructionCheck],
        tool_trace: list,
        transcript_scene: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
    ) -> list[AuditFinding]:
        """判定条件性指令(TOOL_REQUIREMENT / REFERENCE_USAGE)的三态结论。

        每条指令先走确定性 ``match_tool_call``(命中 tool_trace → PASS,不调 LLM);
        未命中的批量交 LLM 判"场景是否该触发该指令",据此分 FAIL(该做没做) /
        N/A(场景不涉及);LLM 失败/异常/解析失败 → 标 N/A(宁少报不乱报)。未命中全量交 LLM,
        >_MAX_LLM_IN_BATCH 由 ``_llm_judge_applicable`` 内部分批循环(无超限兜底 N/A)。

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

        # 未命中全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_applicable 内部分批循环,
        # 不再单次截断 + 超限保守 FAIL)。
        applicable_mask = self._llm_judge_applicable(
            [inst for _, inst in unmatched], transcript_scene, model, timeout, on_progress,
        )

        for local_pos, (orig_i, inst) in enumerate(unmatched):
            tool_name = inst.params.get("tool_name", "")
            script_path = inst.params.get("script_path", "")
            miss_desc = f"调用 {tool_name}" if tool_name else f"读取 {script_path}"
            verdict, reason = applicable_mask[local_pos]
            if verdict == AuditVerdict.FAIL:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    evidence=f"场景应触发但 trace 未{miss_desc}",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.NA:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    evidence="场景不涉及该指令",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[orig_i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.CONDITIONAL,
                    evidence=reason,
                    transcript=transcript_name,
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/该条未被提及/indeterminate 无 reason)
                # → 没判成(UNRESOLVED):宁少报不乱报,判不准不报 FAIL,FAIL 只留给 LLM 明确判"该做没做"。
                findings[orig_i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否该触发 → 没判成"
                )

        return [f for f in findings if f is not None]

    # ---- 禁令类:主动检测违规 -------------------------------------------------

    def _judge_prohibition(
        self,
        instructions: list[InstructionCheck],
        transcript_text: str,
        transcript_name: str,
        model: str | None = None,
        timeout: int = 600,
        on_progress: ProgressFn | None = None,
    ) -> list[AuditFinding]:
        """判定禁令类指令(CONSTRAINT / ANTI_FABRICATION)——**主动检测违规是否发生**。

        与条件性/步骤判"该不该做/做没做"不同,禁令类问"这次真实执行里有没有发生违反
        该禁令/约束的行为"(如编造数据、跳过必须步骤、用了被禁止的做法)。批量交 LLM,
        每条返回 ``{index, violated: bool, evidence}``:
        - violated=True  → FAIL(违规了)
        - violated=False → PASS(没违规)
        - LLM 异常 / 解析失败 / 未提及 / violated 缺键或非布尔 → **N/A**(宁少报不乱报:
          即便禁令也在无确证时不报 FAIL;严格 ``is True/is False`` 判断,畸形值不当 PASS)。
          全量交 LLM,>_MAX_LLM_IN_BATCH 由 ``_llm_judge_prohibition`` 内部分批循环(无超限兜底 N/A)。

        Args:
            instructions: 已筛出的禁令类指令(CONSTRAINT / ANTI_FABRICATION)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 PROHIBITION。
        """
        if not instructions:
            return []  # 空桶短路:不 spawn LLM(与 audit"零重跑低成本"相悖则不调)

        findings: list[AuditFinding | None] = [None] * len(instructions)

        # 全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_prohibition 内部分批循环)。
        violated_mask = self._llm_judge_prohibition(
            instructions, transcript_text, model, timeout, on_progress,
        )

        for i, inst in enumerate(instructions):
            verdict, ev = violated_mask[i]
            if verdict == AuditVerdict.FAIL:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中发生了违反该禁令的行为",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.PASS:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev or "执行中未违反该禁令",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.PROHIBITION,
                    evidence=ev,
                    transcript=transcript_name,
                )
            else:
                # None:LLM 未给明确结论(调用失败/解析失败/未提及/畸形值/indeterminate 无 evidence)
                # → 没判成(UNRESOLVED):宁少报不乱报,即便禁令也在无确证时不报 FAIL;
                # audit() 汇总"建议重跑"警告提示人工复核,不靠假 FAIL 充数。
                findings[i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否违规 → 没判成",
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
        timeout: int = 600,
        tool_trace: list | None = None,
        on_progress: ProgressFn | None = None,
    ) -> list[AuditFinding]:
        """判定步骤类指令(HEADING_STEP)的三态结论。

        批量交 LLM,每条返回 ``{index, verdict: "pass"|"fail"|"na", evidence}``,
        判定标准="这次执行有没有走这一步:走了→pass;该走没走→fail;场景不涉及该步→na":
        - pass → PASS
        - fail → FAIL
        - na   → N/A
        - 未知 verdict / LLM 异常 / 解析失败 → **N/A**(宁少报不乱报:判不准不报 FAIL)。
          全量交 LLM,>_MAX_LLM_IN_BATCH 由 ``_llm_judge_step`` 内部分批循环(无超限兜底 N/A)。

        verdict 匹配大小写不敏感(strip+lower);未知值/非字符串 → 标 N/A。
        空桶(0 条)→ 短路返回 [],不调 LLM。

        Args:
            instructions: 已筛出的步骤类指令(HEADING_STEP)。
            transcript_text: 该 transcript 的执行记录文本(截断 [:8000] 防 prompt 过长)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。
            model: LLM 模型名(None 走 call_claude 默认)。
            timeout: LLM 调用超时秒数。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 STEP。
        """
        if not instructions:
            return []  # 空桶短路:不 spawn LLM

        findings: list[AuditFinding | None] = [None] * len(instructions)

        # 全量交 LLM 判(>_MAX_LLM_IN_BATCH 由 _llm_judge_step 内部分批循环)。
        verdict_mask = self._llm_judge_step(
            instructions, transcript_text, model, timeout, tool_trace, on_progress,
        )

        for i, inst in enumerate(instructions):
            verdict, ev = verdict_mask[i]
            if verdict == AuditVerdict.PASS:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.PASS,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "执行中走过了该步",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.FAIL:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.FAIL,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "该走这一步但未走",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.NA:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.NA,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev or "场景不涉及该步",
                    transcript=transcript_name,
                )
            elif verdict == AuditVerdict.INDETERMINATE:
                findings[i] = AuditFinding(
                    instruction_text=inst.instruction_text,
                    source=inst.source.value,
                    verdict=AuditVerdict.INDETERMINATE,
                    method=AuditMethod.LLM,
                    category=AuditCategory.STEP,
                    evidence=ev,
                    transcript=transcript_name,
                )
            else:
                # None/未知值/非字符串/indeterminate 无 evidence → 没判成(UNRESOLVED):
                # 宁少报不乱报,判不准不报 FAIL。
                findings[i] = self._make_unresolved_finding(
                    inst, transcript_name, "LLM 判定失败,无法确证是否走过 → 没判成",
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
        ``transcript_text`` 命中 → PASS,否则 FAIL(照已删的 ``compliance.checker._check_programmatic``
        的 PATTERN_MATCH 分支逻辑)。method=PROGRAMMATIC(确定性程序判定,不调 LLM)。

        Args:
            instructions: 已筛出的 OUTPUT_FORMAT 指令(params 含 sample)。
            transcript_text: 该 transcript 的执行记录文本(完整传入,作子串匹配源)。
            transcript_name: 该 transcript 的文件名,填进 finding.transcript。

        Returns:
            每条指令一个 AuditFinding,顺序与输入一致;category 全 OUTPUT;method=PROGRAMMATIC。
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
                    method=AuditMethod.PROGRAMMATIC,
                    category=AuditCategory.OUTPUT,
                    evidence=f"输出含期望格式 {matched[:80]}",
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
                    method=AuditMethod.PROGRAMMATIC,
                    category=AuditCategory.OUTPUT,
                    evidence=evidence,
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
        """命中 tool_trace 的 PASS finding(method=PROGRAMMATIC)。evidence 按 params 里设的键选措辞。"""
        if tool_name:
            evidence = f"trace 中调用了 {tool_name}"
        else:
            evidence = f"trace 中读取了 {script_path}"
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source=inst.source.value,
            verdict=AuditVerdict.PASS,
            method=AuditMethod.PROGRAMMATIC,
            category=AuditCategory.CONDITIONAL,
            evidence=evidence,
            transcript=transcript_name,
        )

    @staticmethod
    def _make_unresolved_finding(
        inst: InstructionCheck,
        transcript_name: str,
        evidence: str,
        category: AuditCategory = AuditCategory.CONDITIONAL,
    ) -> AuditFinding:
        """没判成(UNRESOLVED)finding:LLM 失败 / 解析失败 / 畸形值 / 未提及 / indeterminate 无 reason。

        与 §48 当初的"保守 FAIL"相反:判不准时**不报 FAIL**(FAIL 必须站得住——LLM
        明确判违规 / 该做没做)。操作性失败 → UNRESOLVED(独立于 N/A:N/A 是"不适用"已解决,
        UNRESOLVED 是"没判成"未解决、**可重跑**;另与 INDETERMINATE 区分:INDETERMINATE 是
        LLM 看了证据真判不了、重跑无益)。method=LLM(该条走了 LLM 路径但没出明确结论)。
        ``audit()`` 据此汇总"建议重跑"警告让人知道本次结果可能不完整。``category`` 默认
        CONDITIONAL,prohibition / step 各自传入对应类别,以反映真实指令类别。
        """
        return AuditFinding(
            instruction_text=inst.instruction_text,
            source=inst.source.value,
            verdict=AuditVerdict.UNRESOLVED,
            method=AuditMethod.LLM,
            category=category,
            evidence=evidence,
            transcript=transcript_name,
        )

    # ---- LLM 场景触发判定 ----------------------------------------------------

    def _llm_judge_applicable(
        self,
        candidates: list[InstructionCheck],
        transcript_scene: str,
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
    ) -> list[tuple[AuditVerdict | None, str]]:
        """分批 call_claude 判每条未命中指令在该场景是否"该触发"(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回与 candidates 等长的列表,每元素 ``(verdict, reason)``:
        - (FAIL, "")     = 该触发(该做没做)
        - (NA, "")       = 不涉及(场景不触发)
        - (INDETERMINATE, reason) = 判不准(LLM 看了场景但证据真矛盾/缺失,带具体 reason)
        - (None, "")     = 没判成(调用/解析失败/未提及/indeterminate 无 reason)

        按 ``_MAX_LLM_IN_BATCH`` 分批循环:每批独立 call_claude + ``_parse_applicable``,
        合并成完整 mask(长度=len(candidates))。批内 prompt 的 index 从 0 计(本地 index),
        ``_parse_applicable`` 只认 ``[0, bn)`` 的本地 index;批结果按 ``mask[start:start+bn]``
        写回全量 mask 的对应区段——**绝不**用批内 index 直接当下标(会错位/覆盖)。
        某批调用异常(RuntimeError)→ 先重试 ``_LLM_RETRY_MAX`` 次,仍失败则该批保持
        (None, "")(调用方标 UNRESOLVED + audit 汇总"建议重跑"警告),其余批照常处理。
        空 candidates → range 空 → 不调 LLM,返回 [](调用方 ``_judge_conditional`` 已在
        unmatched 为空时短路,这里作兜底)。
        """
        n = len(candidates)
        mask: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            prompt = (
                "判断以下每条条件性指令在给定场景下是否应该被触发执行。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- ✅ true(该触发):这个场景满足指令的隐含条件——执行者本应做该动作"
                "(如场景涉及该工具应被调用、该参考文件应被读取),却没做 = 疑似违规。\n"
                "- ❌ false(不涉及):这个场景根本不涉及该指令(场景未触及该工具 / 参考"
                "文件的适用范围,本就不该触发)。\n"
                "- ⚠ indeterminate(判不准):你看了场景,但证据真的相互矛盾或关键信息缺失,"
                "判断不了该不该触发。**必须**在 reason 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"## 场景描述\n{transcript_scene[:_SCENE_TRUNCATE]}\n\n"
                f"## 待判指令 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "applicable": true/false, "indeterminate": true(仅判不准时), '
                '"reason": "indeterminate 时必填:具体矛盾/缺失的证据"}\n'
            )
            parsed = _judge_batch_with_retry(
                prompt, model, timeout, self._parse_applicable, bn,
                on_progress=on_progress, label="条件性", k=k, total=total,
                is_unparsed=lambda v: v[0] is None,
            )
            if parsed is _RETRY_EXHAUSTED:
                continue  # 重试耗尽 → 保留 mask 预置的全 (None,"")(audit 汇总"建议重跑"警告)
            for j, val in enumerate(parsed):
                mask[start + j] = val  # 本地 j → 全量 start+j(批间 index 映射)
        return mask

    @staticmethod
    def _parse_applicable(text: str, n: int) -> list[tuple[AuditVerdict | None, str]]:
        """解析 LLM 场景触发判定 → ``(AuditVerdict | None, reason)``;默认 ``(None, "")`` = 没判成。

        - applicable=true  → (FAIL, "")(该做没做)
        - applicable=false → (NA, "")(场景不涉及)
        - indeterminate=true 且 reason 非空 → (INDETERMINATE, reason)(判不准)
        - indeterminate=true 但 reason 空 → **滥用闸 ①** 降级 (None, "") = 没判成
          (不许空口"判不准",必须带具体矛盾/缺失证据)
        - 其余(缺键/None/非布尔/未提及)→ (None, "") = 没判成(宁少报不乱报,不报假 FAIL)

        严格 ``is True/is False`` 判断(畸形值如字符串 "false" 不洗成明确结论)。
        """
        mask: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
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
                if item.get("indeterminate") is True:
                    reason = str(item.get("reason", "") or "").strip()
                    if reason:
                        mask[idx] = (AuditVerdict.INDETERMINATE, reason)
                    # else: indeterminate 无具体 reason → 滥用闸 ① 降级没判成(保持 (None,""))
                    continue
                val = item.get("applicable")
                if val is True:
                    mask[idx] = (AuditVerdict.FAIL, "")
                elif val is False:
                    mask[idx] = (AuditVerdict.NA, "")
                # else: 缺 applicable / None / 非布尔 → 保持 (None,"") = 没判成
        return mask

    # ---- LLM 精筛伪指令(extract 后、判定前)-------------------------------

    def _refine_pseudo_instructions(
        self,
        instructions: list[InstructionCheck],
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
    ) -> tuple[list[InstructionCheck], list[str]]:
        """LLM 精筛:砍掉 extractor 误抽的示例/占位文字(非真实可检查指令)。

        正则粗召回保召回,这里提精度:对每条候选(除 OUTPUT_FORMAT——它是纯程序
        子串匹配,精筛无收益)问 LLM「真实可检查的指令 vs 示例/占位」,只砍明确
        ``is_real=False`` 的。**精度 > 召回**:True/None(没判成/判不准/整批失败)
        全保留——宁可留伪指令让下游判 N/A,也别砍真指令。

        返回 ``(精筛后指令列表, warnings)``:warnings 报滤除数(按 source 分),
        无砍则空。per-skill 调一次(audit() 在 per-transcript 循环外调它)。
        """
        # 候选 = 除 OUTPUT_FORMAT(PATTERN_MATCH 程序判定)外的全部
        cand_idx = [
            i for i, inst in enumerate(instructions)
            if inst.source != InstructionSource.OUTPUT_FORMAT
        ]
        candidates = [instructions[i] for i in cand_idx]
        if not candidates:
            return instructions, []  # 无候选,不调 LLM
        mask = self._llm_classify_real(candidates, model, timeout, on_progress)
        keep = [True] * len(instructions)  # 默认全保留(精度>召回)
        dropped_src: list[str] = []
        for k, verdict in enumerate(mask):
            if verdict is False:  # 只砍明确 is_real=False
                keep[cand_idx[k]] = False
                dropped_src.append(candidates[k].source.value)
        refined = [inst for inst, k in zip(instructions, keep) if k]
        warns: list[str] = []
        if dropped_src:
            from collections import Counter
            parts = ", ".join(f"{src} {n}" for src, n in Counter(dropped_src).items())
            warns.append(f"refine: 滤除 {len(dropped_src)} 条疑似示例/占位伪指令({parts})")
        return refined, warns

    def _llm_classify_real(
        self,
        candidates: list[InstructionCheck],
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
    ) -> list[bool | None]:
        """分批 call_claude 判每条候选「真实可检查指令 vs 示例/占位」(每批 _MAX_LLM_IN_BATCH 条)。

        返回与 candidates 等长,每元素:True=真指令(留)/ False=示例占位(砍)/
        None=没判成(调用/解析失败/indeterminate 判不准)→ 调用方保守保留。
        镜像 ``_llm_judge_applicable`` 的分批循环 + ``_judge_batch_with_retry`` + ``mask[start+j]``。
        """
        n = len(candidates)
        mask: list[bool | None] = [None] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] (来源 {inst.source.value} / 标题:\"{inst.section}\") {inst.instruction_text}\n"
                for i, inst in enumerate(batch)
            )
            prompt = (
                "判断以下每条从 SKILL.md 抽出的候选文本:它是**真实可检查的指令**"
                "(要求执行者做/不做某件可在执行记录里对账的事),"
                "还是**示例/演示结果/占位/描述性文字**?\n\n"
                "## 判定标准(优先给出明确结论;只有真的分不清时才用 indeterminate)\n"
                "- ✅ is_real=true(真指令):祈使句/约束/要求,可判做了没做"
                "(如\"Step 1: 读取 X\"、\"NEVER 编造数据\"、\"运行 validate.py\")。\n"
                "- ❌ is_real=false(非指令):教程示例文字、示例输出结果、说明性描述、"
                "占位文本(如\"3 agents dispatched in parallel\"是演示结果、"
                "\"6 failures across 3 files\"是例句)。\n"
                "- ⚠ indeterminate(分不清):只在真的无法判断时用,**必须**在 reason 写明;"
                "不要用 indeterminate 偷懒回避。\n\n"
                f"## 待判候选 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "is_real": true/false, '
                '"indeterminate": true(仅分不清时), "reason": "indeterminate 时必填"}\n'
            )
            parsed = _judge_batch_with_retry(
                prompt, model, timeout, self._parse_real, bn,
                on_progress=on_progress, label="精筛", k=k, total=total,
                is_unparsed=lambda v: v is None,
            )
            if parsed is _RETRY_EXHAUSTED:
                continue  # 重试耗尽 → 保留 mask 预置的全 None(调用方全保留)
            for j, val in enumerate(parsed):
                mask[start + j] = val
        return mask

    @staticmethod
    def _parse_real(text: str, n: int) -> list[bool | None]:
        """解析 LLM 真伪指令判定 → ``bool | None``;默认 ``None`` = 没判成(调用方保留)。

        - is_real=true  → True(真指令)
        - is_real=false → False(示例/占位 → 砍)
        - indeterminate=true → None(分不清 → 保守保留,不砍)
        - 其余(缺键/None/非布尔/未提及)→ None(没判成 → 保留)
        严格 ``is True/is False`` 判断(畸形值如字符串 "false" 不洗成明确结论)。
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
                if item.get("indeterminate") is True:
                    continue  # 分不清 → 保持 None(保留)
                val = item.get("is_real")
                if val is True:
                    mask[idx] = True
                elif val is False:
                    mask[idx] = False
                # else: 缺 is_real / None / 非布尔 → 保持 None(保留)
        return mask

    # ---- 禁令类 LLM 违规检测 -------------------------------------------------

    def _llm_judge_prohibition(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
        on_progress: ProgressFn | None = None,
    ) -> list[tuple[AuditVerdict | None, str]]:
        """分批 call_claude 判每条禁令在本次执行里是否被违反(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回与 candidates 等长的列表,每元素 ``(verdict, evidence)``:
        - (FAIL, ev)          = 已违反
        - (PASS, ev)          = 未违反
        - (INDETERMINATE, ev) = 判不准(LLM 看了证据真矛盾/缺失,带具体 evidence)
        - (None, "")          = 没判成(调用/解析失败/未提及/畸形值/indeterminate 无 evidence)

        分批模式同 ``_llm_judge_applicable``:每批独立 call_claude + ``_parse_prohibition``,
        批内 index 从 0 计、结果按 ``results[start:start+bn]`` 写回全量(批间 index 映射,
        绝不用批内 index 直接当下标)。某批 RuntimeError → 先重试 ``_LLM_RETRY_MAX`` 次,
        仍失败则该批保持 (None, "")(调用方标 UNRESOLVED + "建议重跑"警告),
        其余批继续。空 candidates → 不调 LLM,返回 []。
        """
        n = len(candidates)
        results: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            prompt = (
                "判断以下每条禁令/约束在本次真实执行里是否被违反。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- ✅ violated=true(已违反):这次执行里确实发生了违反该禁令/约束的行为"
                "(如编造了数据、跳过了必须步骤、用了被禁止的做法)。\n"
                "- ❌ violated=false(未违反):这次执行没有违反该禁令/约束。\n"
                "- ⚠ indeterminate=true(判不准):你看了执行记录,但证据真的相互矛盾或关键信息缺失,"
                "判断不了是否违反。**必须**在 evidence 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"## 执行记录摘录\n{transcript_text[:_SCENE_TRUNCATE]}\n\n"
                f"## 待判禁令 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "violated": true/false, "indeterminate": true(仅判不准时), '
                '"evidence": "中文简述为何违反/未违反/判不准(具体到证据)"}\n'
            )
            parsed = _judge_batch_with_retry(
                prompt, model, timeout, self._parse_prohibition, bn,
                on_progress=on_progress, label="禁令", k=k, total=total,
                is_unparsed=lambda v: v[0] is None,
            )
            if parsed is _RETRY_EXHAUSTED:
                continue  # 重试耗尽 → 保留 results 预置的全 (None,"")(audit 汇总"建议重跑"警告)
            for j, val in enumerate(parsed):
                results[start + j] = val  # 本地 j → 全量 start+j
        return results

    @staticmethod
    def _parse_prohibition(
        text: str, n: int
    ) -> list[tuple[AuditVerdict | None, str]]:
        """解析 LLM 禁令违规判定 → ``(AuditVerdict | None, evidence)``;默认 ``(None, "")`` = 没判成。

        - violated=true  → (FAIL, ev)
        - violated=false → (PASS, ev)
        - indeterminate=true 且 evidence 非空 → (INDETERMINATE, ev)(判不准)
        - indeterminate=true 但 evidence 空 → **滥用闸 ①** 降级 (None, "") = 没判成
        - 其余(缺键/None/非布尔/未提及)→ (None, "") = 没判成

        与 ``_parse_applicable`` 同构(同 JSON 格式 + extract_first_balanced + 严格
        ``is True/is False``),只是多取一个 evidence 字段。畸形值(如字符串 "true")
        不用 bool() 洗,保持 (None, "") = 没判成(不误判违规)。
        """
        results: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
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
                ev = str(item.get("evidence", "") or "")
                if item.get("indeterminate") is True:
                    if ev.strip():
                        results[idx] = (AuditVerdict.INDETERMINATE, ev)
                    # else: indeterminate 无 evidence → 滥用闸 ① 降级没判成(保持 (None,""))
                    continue
                violated = item.get("violated")
                if violated is True:
                    results[idx] = (AuditVerdict.FAIL, ev)
                elif violated is False:
                    results[idx] = (AuditVerdict.PASS, ev)
                # else: 缺 violated 键 / None / 非布尔 → 保持 (None,"") = 没判成
        return results

    # ---- 步骤类 LLM 三态判定 -------------------------------------------------

    def _llm_judge_step(
        self,
        candidates: list[InstructionCheck],
        transcript_text: str,
        model: str | None,
        timeout: int,
        tool_trace: list | None = None,
        on_progress: ProgressFn | None = None,
    ) -> list[tuple[AuditVerdict | None, str]]:
        """分批 call_claude 判每个步骤在本次执行里是否被走过(每批 _MAX_LLM_IN_BATCH(10) 条,处理全部)。

        返回与 candidates 等长的列表,每元素 ``(verdict, evidence)``:
        - (PASS, ev)          = 走过
        - (FAIL, ev)          = 该走没走
        - (NA, ev)            = 场景不涉及
        - (INDETERMINATE, ev) = 判不准(LLM 看了证据真矛盾/缺失,带具体 evidence)
        - (None, "")          = 没判成(调用/解析失败/未提及/未知 verdict/非字符串/indeterminate 无 evidence)

        分批模式同 ``_llm_judge_applicable``:每批独立 call_claude + ``_parse_step``,
        批内 index 从 0 计、结果按 ``results[start:start+bn]`` 写回全量(批间 index 映射,
        绝不用批内 index 直接当下标)。某批 RuntimeError → 先重试 ``_LLM_RETRY_MAX`` 次,
        仍失败则该批保持 (None, "")(调用方标 UNRESOLVED + "建议重跑"警告),
        其余批继续。空 candidates → 不调 LLM,返回 []。
        """
        n = len(candidates)
        results: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
        total = (n + _MAX_LLM_IN_BATCH - 1) // _MAX_LLM_IN_BATCH
        for start in range(0, n, _MAX_LLM_IN_BATCH):
            batch = candidates[start:start + _MAX_LLM_IN_BATCH]
            bn = len(batch)
            k = start // _MAX_LLM_IN_BATCH + 1
            instruction_list = "".join(
                f"[{i}] {inst.instruction_text}\n" for i, inst in enumerate(batch)
            )
            # tool_trace 进 prompt:判涉及工具的步骤时让 LLM 看到实际工具调用,而非只凭
            # assistant 文字声称(canary 暴露的缺口 —— 否则"Read input log"步骤明明调了
            # Read 却因 transcript_text 只剩文字声称而误判 FAIL)。
            tool_section = ""
            if tool_trace:
                tool_lines = "\n".join(f"- {t.name}: {t.input_summary}" for t in tool_trace)
                tool_section = (
                    "## 实际工具调用记录(判涉及工具的步骤时以此为准,勿只凭文字声称)\n"
                    f"{tool_lines}\n\n"
                )
            prompt = (
                "判断以下每个步骤在本次执行里是否被走过。\n\n"
                "## 判定标准(优先给出明确结论;只有证据真的相互矛盾或关键缺失时才用 indeterminate)\n"
                "- pass:这次执行走过了这一步。\n"
                "- fail:这个场景本该走这一步但没走。\n"
                "- na:这个场景根本不涉及该步骤。\n"
                "- indeterminate:你看了执行记录,但证据真的相互矛盾或关键信息缺失,"
                "判断不了走没走。**必须**在 evidence 写明具体矛盾 / 缺失的证据;"
                "不要用 indeterminate 偷懒回避判断。\n\n"
                f"## 执行记录摘录\n{transcript_text[:_SCENE_TRUNCATE]}\n\n"
                f"{tool_section}"
                f"## 待判步骤 ({bn} 条)\n{instruction_list}\n"
                "## 输出格式\n"
                '只返回 JSON 数组,每元素:'
                '{"index": 0, "verdict": "pass"|"fail"|"na"|"indeterminate", '
                '"evidence": "中文简述(具体到证据)"}\n'
            )
            parsed = _judge_batch_with_retry(
                prompt, model, timeout, self._parse_step, bn,
                on_progress=on_progress, label="步骤", k=k, total=total,
                is_unparsed=lambda v: v[0] is None,
            )
            if parsed is _RETRY_EXHAUSTED:
                continue  # 重试耗尽 → 保留 results 预置的全 (None,"")(audit 汇总"建议重跑"警告)
            for j, val in enumerate(parsed):
                results[start + j] = val  # 本地 j → 全量 start+j
        return results

    @staticmethod
    def _parse_step(
        text: str, n: int
    ) -> list[tuple[AuditVerdict | None, str]]:
        """解析 LLM 步骤判定 → ``(AuditVerdict | None, evidence)``;默认 ``(None, "")`` = 没判成。

        verdict 匹配大小写不敏感(strip + lower):
        - "pass"/"fail"/"na" → (PASS/FAIL/NA, ev)
        - "indeterminate" 且 evidence 非空 → (INDETERMINATE, ev)(判不准)
        - "indeterminate" 但 evidence 空 → **滥用闸 ①** 降级 (None, "") = 没判成
        - 未知值 / 非字符串 / 缺键 → (None, "") = 没判成(不误判 N/A 放过疑似漏步)
        """
        results: list[tuple[AuditVerdict | None, str]] = [(None, "")] * n
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
                    if vl == "pass":
                        results[idx] = (AuditVerdict.PASS, ev)
                    elif vl == "fail":
                        results[idx] = (AuditVerdict.FAIL, ev)
                    elif vl == "na":
                        results[idx] = (AuditVerdict.NA, ev)
                    elif vl == "indeterminate":
                        if ev.strip():
                            results[idx] = (AuditVerdict.INDETERMINATE, ev)
                        # else: indeterminate 无 evidence → 滥用闸 ① 降级没判成(保持 (None,""))
                    # else: 未知 verdict 字符串 → 保持 (None,"") = 没判成
                # else: 缺 verdict 键 / 非字符串 → 保持 (None,"") = 没判成
        return results
