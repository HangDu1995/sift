"""Batch, compliance, calibration, optimization, security, and overfitting models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from sift.models.analysis_models import AnalysisResult
from sift.models.benchmark_models import (
    AnomalyReport,
    ComplianceSummary,
    RunResultEntry,
    StatsValue,
)
from sift.models.comparison_models import ComparisonResult
from sift.models.enums import (
    CalibrationVerdict,
    CheckMethod,
    InstructionPriority,
    InstructionSource,
    ProcessStatus,
    ScannerStatus,
    SecuritySeverity,
    SecurityVerdict,
)
from sift.models.eval_models import Assertion
from sift.stats import wilson_ci


class BenchmarkMetadata(BaseModel):
    skill_name: str
    skill_path: str = ""
    executor_model: str = ""
    analyzer_model: str = ""
    comparator_model: str = ""
    timestamp: str = ""
    evals_run: list[int] = Field(default_factory=list)
    runs_per_configuration: int = 3


class ComparisonSummary(BaseModel):
    """盲比较聚合(主观侧并列主位)。win-rate 有效分母 = a+b+genuine tie;
    failed(调用/解析失败、重试耗尽)排除出分母——失败≠打平。

    a/b = side A/B。label_a/b 默认 "with"/"without"(with/without 模式);
    v1/v2 模式传 v1/v2。win_rate 恒为 side A 胜率(a_wins/valid)。
    """

    total: int = 0
    a_wins: int = 0
    b_wins: int = 0
    ties: int = 0  # genuine tie(有效,计入分母)
    failed: int = 0  # 无信号,排除出分母
    valid: int = 0  # a_wins + b_wins + ties
    win_rate: float | None = None  # a_wins / valid;valid==0 → None
    win_rate_ci_lo: float | None = None  # Wilson 95% 下界(valid==0 → None);lo>0.5 → A 显著更优
    win_rate_ci_hi: float | None = None  # Wilson 95% 上界;hi<0.5 → B 显著更优;含 0.5 → 不显著
    label_a: str = "with"
    label_b: str = "without"

    @classmethod
    def from_comparisons(
        cls,
        comparisons: list,
        label_a: str = "with",
        label_b: str = "without",
    ) -> "ComparisonSummary | None":
        if not comparisons:
            return None

        def _is(c, w):
            return c.winner == w and not getattr(c, "failed", False)

        a_wins = sum(1 for c in comparisons if _is(c, label_a))
        b_wins = sum(1 for c in comparisons if _is(c, label_b))
        # genuine tie:winner 非 label_a/b 且非 failed(失败-tie 因 failed=True 被排除)
        ties = sum(1 for c in comparisons if c.winner not in (label_a, label_b) and not getattr(c, "failed", False))
        failed = sum(1 for c in comparisons if getattr(c, "failed", False))
        valid = a_wins + b_wins + ties
        lo, hi = wilson_ci(a_wins, valid)  # valid==0 → (None, None)
        return cls(
            total=len(comparisons),
            a_wins=a_wins,
            b_wins=b_wins,
            ties=ties,
            failed=failed,
            valid=valid,
            win_rate=a_wins / valid if valid else None,
            win_rate_ci_lo=lo,
            win_rate_ci_hi=hi,
            label_a=label_a,
            label_b=label_b,
        )


class BenchmarkData(BaseModel):
    metadata: BenchmarkMetadata
    runs: list[RunResultEntry] = Field(default_factory=list)
    run_summary: dict[str, Any] = Field(default_factory=dict)
    notes: list[Any] = Field(default_factory=list)
    comparisons: list[ComparisonResult] = Field(default_factory=list)
    comparison_summary: ComparisonSummary | None = None
    analysis: AnalysisResult | None = None
    anomaly_report: AnomalyReport | None = None
    calibration_summary: CalibrationSummary | None = None
    selection: dict[str, Any] | None = None  # 选题信息(如 changed-from 子集跑:mode/ids/affected/total)
    llm_token_usage: dict[str, Any] = Field(default_factory=dict)  # Phase 2: {scoring, audit, executor, total}
    llm_duration_seconds: dict[str, Any] = Field(
        default_factory=dict
    )  # Phase 2.5: {scoring, audit, executor, total};total=墙钟(非分列和)


class InstructionCheck(BaseModel):
    source: InstructionSource
    instruction_text: str
    section: str
    priority: InstructionPriority
    check_method: CheckMethod
    params: dict[str, Any] = Field(default_factory=dict)
    # 该指令在声明原文里所属 markdown 列表项(含续行)的完整文本,带主语上下文。
    # **判定不读它**:judge 吃 instruction_text(谓语片段,如「必须存在」);source_excerpt
    # 只供显示层补全被切走的主语(如「design_doc_cases.json 必须存在」)。extract 时填,
    # 透传到 AuditFinding;缺省空(向后兼容旧产物 / 合成文案)。
    source_excerpt: str = ""
    # source_excerpt 的「人话改写」:LLM 把原文 bullet 去噪(反引号/括号注释/换行)、补主语、
    # 压成一句顺口标题,供显示优先取用。独立 LLM 步骤产(refine 之后、judge 之前),独立缓存。
    # **判定不读**:judge 仍吃 instruction_text。缺省空(关 --headline / 未生成 / 旧产物)→
    # 显示回退 source_excerpt → instruction_text。
    headline: str = ""
    # 指令 match 在声明原文里所处的**真实 markdown 标题**。body 级 extractor(约束/防编造/
    # 参考文件,跨章节扫描)的 ``section`` 是伪节名("Constraint" 等),真实归属标题由此字段
    # 回填;标题级 extractor(HEADING_STEP / TOOL_REQUIREMENT / OUTPUT_FORMAT)的 ``section``
    # 已是真标题,本字段留空。**判定不读、缓存键不含它**(refine/headline 键只吃 source|section|
    # instruction_text[|source_excerpt],本字段缺省不影响命中);显示层 finding.section =
    # source_section or section。缺省空(旧 refine 缓存条目 / 旧 JSON)→ 回退伪节名。
    source_section: str = ""


class HardViolation(BaseModel):
    """硬类(prohibition/output)违规明细,供人秒辨真伪。"""

    category: str  # AuditCategory 值:"prohibition" | "output"
    instruction_text: str
    evidence: str  # AuditFinding.evidence(为何 FAIL,中文)


class CalibrationReport(BaseModel):
    """单次 with_skill 执行的过程 x 结果 coherence 判决。
    gate 不读本结构(只 viewer 显示用)。verdict 由结果(客观 pass_rate>=0.8 优先,无客观锚回退 mixed)
    x 过程(硬类违规)矩阵决定。
    """

    verdict: CalibrationVerdict
    result_pass: bool
    process_status: ProcessStatus
    hard_violations: list[HardViolation] = Field(default_factory=list)
    soft_notes: dict[str, int] = Field(default_factory=dict)  # {"step": N, "conditional": N} = FAIL 计数
    scoring_rate: float | None  # 驱动判决的率:客观优先,无客观锚回退 mixed
    scoring_rate_basis: str  # "programmatic"(押客观)| "mixed"(无客观锚,含主观)
    scoring_rate_programmatic: float | None  # 客观通过率(参考;None=该 run 无客观断言)
    scoring_rate_llm: float | None  # 主观通过率(参考)
    compliance_rate: float | None  # = pass/(pass+fail)(显示用);过程未知时 None


class PerSideCalibration(BaseModel):
    """单 side 的 calibration 聚合。high_score_suspects 只收 HIGH_SCORE_SUSPECT(gate 盲区);
    SKILL_FAILED / FIXABLE 结果错归客观轴,不收。
    """

    total: int = 0
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    high_score_suspects: list[dict[str, Any]] = Field(default_factory=list)
    # 每项 {eval, run, category, instruction_text, evidence, compliance_rate}


class CalibrationSummary(BaseModel):
    """per-side calibration 聚合(对称 ComparisonSummary)。
    with/without:without_skill 侧 None(无 compliance);v1/v2 两侧都有。
    """

    sides: dict[str, PerSideCalibration | None] = Field(default_factory=dict)

    @classmethod
    def from_per_side(
        cls,
        per_side: dict[str, list[tuple[int, int, "CalibrationReport"]] | None],
    ) -> "CalibrationSummary":
        out: dict[str, PerSideCalibration | None] = {}
        for side, reports in per_side.items():
            if not reports:
                out[side] = None
                continue
            counts: dict[str, int] = {}
            suspects: list[dict[str, Any]] = []
            for eval_id, run_num, rep in reports:
                v = rep.verdict.value if hasattr(rep.verdict, "value") else str(rep.verdict)
                counts[v] = counts.get(v, 0) + 1
                if not (v == "HIGH_SCORE_SUSPECT"):
                    continue
                for hv in rep.hard_violations:
                    suspects.append(
                        {
                            "eval": eval_id,
                            "run": run_num,
                            "category": hv.category,
                            "instruction_text": hv.instruction_text,
                            "evidence": hv.evidence,
                            "compliance_rate": rep.compliance_rate,
                        }
                    )
            out[side] = PerSideCalibration(
                total=len(reports),
                verdict_counts=counts,
                high_score_suspects=suspects,
            )
        return cls(sides=out)


class SecurityFinding(BaseModel):
    scanner: str
    severity: SecuritySeverity
    description: str
    file_path: str = ""
    line: int | None = None


class ScannerResult(BaseModel):
    name: str
    status: ScannerStatus
    findings: list[SecurityFinding] = Field(default_factory=list)
    message: str = ""


class SecurityReport(BaseModel):
    skill_name: str
    skill_path: str
    verdict: SecurityVerdict
    scanners: list[ScannerResult] = Field(default_factory=list)
    findings: list[SecurityFinding] = Field(default_factory=list)
    summary: str = ""
