"""tests/test_noise.py — FAIL 噪声/skill 判别(finding 级 + 聚合级)。

audit 的 FAIL finding 里混着两类:工具噪声(extractor 误提资源描述当步骤、evidence 自相矛盾
"不涉及却判 FAIL"、LLM 判定不稳)vs 真正的 skill 问题(硬约束程序判定 FAIL)。本模块给每条
FAIL 标 noise_label(tool_noise / skill_issue / uncertain)+ noise_reasons,帮用户区分、
避免据噪声误改 skill。

纯函数启发式(初版),不调 LLM、不读 transcript——对齐 dedup.py 定位。
"""

from __future__ import annotations

from finding_fixtures import FindingEvidence, FindingOrigin
from report_fixtures import legacy_instruction_aggregate

from sift.audit.noise import annotate_aggregate_noise, annotate_noise
from sift.models.audit_models import (
    AuditCategory,
    AuditFinding,
    AuditMethod,
    AuditVerdict,
    InstructionAggregate,
)

# ---------- factories ----------


def _f(
    text: str = "t",
    verdict: AuditVerdict = AuditVerdict.FAIL,
    transcript: str = "t.jsonl",
    origin: FindingOrigin = FindingOrigin(),
    result: FindingEvidence = FindingEvidence(),
) -> AuditFinding:
    source = origin.source
    category = origin.category
    method = result.method
    evidence = result.text
    related = result.related
    return AuditFinding(
        instruction_text=text,
        source=source,
        verdict=verdict,
        category=category,
        evidence=evidence,
        method=method,
        transcript=transcript,
        related=list(related) if related else [],
    )


def _agg(
    source: str = "HEADING_STEP",
    fail_count: int = 1,
    na_count: int = 0,
    category: AuditCategory = AuditCategory.STEP,
    pass_count: int = 0,
) -> InstructionAggregate:
    total = pass_count + fail_count + na_count
    return InstructionAggregate(
        instruction_text="t",
        source=source,
        category=category,
        pass_count=pass_count,
        fail_count=fail_count,
        na_count=na_count,
        total=total,
        fail_rate=fail_count / total if total else 0.0,
        fail_in_transcripts=["t.jsonl"] if fail_count else [],
    )


# ---------- model 向后兼容 ----------


class TestNoiseModelCompat:
    @staticmethod
    def test_old_audit_finding_json_loads_with_defaults() -> None:
        old = AuditFinding.model_validate(
            {
                "instruction_text": "x",
                "source": "CONSTRAINT",
                "verdict": "FAIL",
                "method": "PROGRAMMATIC",
                "category": "prohibition",
                "evidence": "e",
                "transcript": "t.jsonl",
            }
        )
        assert old.noise_label is None
        assert old.noise_reasons == []

    @staticmethod
    def test_old_instruction_aggregate_json_loads_with_defaults() -> None:
        old = InstructionAggregate.model_validate(legacy_instruction_aggregate("x"))
        assert old.noise_label is None
        assert old.noise_reasons == []


# ---------- annotate_noise (finding 级) ----------


class TestAnnotateNoiseToolNoise:
    @staticmethod
    def test_heading_step_resource_link_is_tool_noise() -> None:
        # HEADING_STEP 且 instruction_text 含资源链接 [..](xxx.md)
        f = _f(
            text="参见 [错误码表](references/error-codes.md) 做分类",
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "tool_noise"
        assert any("资源描述被当步骤" in r for r in out[0].noise_reasons)

    @staticmethod
    def test_evidence_contradiction_is_tool_noise() -> None:
        # verdict=FAIL 但 evidence 称"不涉及"
        f = _f(
            text="某步骤",
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(text="本场景不涉及该步骤,但判 FAIL"),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "tool_noise"
        assert any("不涉及" in r for r in out[0].noise_reasons)

    @staticmethod
    def test_llm_with_related_is_tool_noise() -> None:
        f = _f(
            text="读 references/x.md",
            origin=FindingOrigin(source="REFERENCE_USAGE", category=AuditCategory.CONDITIONAL),
            result=FindingEvidence(method=AuditMethod.LLM, related=["#3"]),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "tool_noise"
        assert any("LLM" in r for r in out[0].noise_reasons)

    @staticmethod
    def test_multi_signal_collects_reasons() -> None:
        # HEADING_STEP + 资源链接 + evidence 矛盾 → tool_noise,reasons 含两条
        f = _f(
            text="参见 [码表](references/error-codes.md)",
            origin=FindingOrigin(source="HEADING_STEP", category=AuditCategory.STEP),
            result=FindingEvidence(text="本场景不涉及该步骤"),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "tool_noise"
        assert len(out[0].noise_reasons) == 2
        assert any("资源描述被当步骤" in r for r in out[0].noise_reasons)
        assert any("不涉及" in r for r in out[0].noise_reasons)


class TestAnnotateNoiseSkillIssue:
    @staticmethod
    def test_constraint_programmatic_is_skill_issue() -> None:
        f = _f(
            text="禁止编造错误码",
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(method=AuditMethod.PROGRAMMATIC, text="检测到编造的错误码"),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "skill_issue"
        assert any("硬约束" in r for r in out[0].noise_reasons)

    @staticmethod
    def test_anti_fabrication_programmatic_is_skill_issue() -> None:
        f = _f(
            text="不要编造",
            origin=FindingOrigin(source="ANTI_FABRICATION", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(method=AuditMethod.PROGRAMMATIC),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "skill_issue"


class TestAnnotateNoiseUncertain:
    @staticmethod
    def test_no_signal_fail_is_uncertain() -> None:
        # OUTPUT_FORMAT + PROGRAMMATIC,无资源链接、无 evidence 矛盾、无 related
        f = _f(
            text="输出 JSON",
            origin=FindingOrigin(source="OUTPUT_FORMAT", category=AuditCategory.OUTPUT),
            result=FindingEvidence(method=AuditMethod.PROGRAMMATIC),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "uncertain"
        assert len(out[0].noise_reasons) == 1


class TestAnnotateNoiseNonFail:
    @staticmethod
    def test_non_fail_findings_get_no_label() -> None:
        for v in (AuditVerdict.PASS, AuditVerdict.NA, AuditVerdict.UNRESOLVED, AuditVerdict.INDETERMINATE):
            f = _f(
                text="x",
                verdict=v,
                origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
                result=FindingEvidence(),
            )
            out = annotate_noise([f])
            assert out[0].noise_label is None, f"verdict={v}"
            assert out[0].noise_reasons == [], f"verdict={v}"


class TestAnnotateNoisePriority:
    @staticmethod
    def test_tool_noise_beats_skill_issue() -> None:
        # 同时满足 tool_noise(evidence 矛盾)和 skill_issue 候选(CONSTRAINT+PROGRAMMATIC)
        # → tool_noise 胜出(最严)
        f = _f(
            text="禁止某事",
            origin=FindingOrigin(source="CONSTRAINT", category=AuditCategory.PROHIBITION),
            result=FindingEvidence(method=AuditMethod.PROGRAMMATIC, text="本场景不涉及"),
        )
        out = annotate_noise([f])
        assert out[0].noise_label == "tool_noise"
        assert all("硬约束" not in r for r in out[0].noise_reasons)


# ---------- annotate_aggregate_noise (聚合级) ----------


class TestAnnotateAggregateNoise:
    @staticmethod
    def test_na_dominates_heading_step_is_tool_noise() -> None:
        a = _agg(source="HEADING_STEP", fail_count=1, na_count=3)
        out = annotate_aggregate_noise([a])
        assert out[0].noise_label == "tool_noise"
        assert any("NA3" in r and "FAIL1" in r for r in out[0].noise_reasons)

    @staticmethod
    def test_balanced_is_uncertain() -> None:
        a = _agg(source="HEADING_STEP", fail_count=2, na_count=1)
        out = annotate_aggregate_noise([a])
        assert out[0].noise_label == "uncertain"

    @staticmethod
    def test_no_fail_not_annotated() -> None:
        a = _agg(source="HEADING_STEP", fail_count=0, na_count=2, pass_count=1)
        out = annotate_aggregate_noise([a])
        assert out[0].noise_label is None
        assert out[0].noise_reasons == []

    @staticmethod
    def test_na_dominates_but_not_heading_step_is_uncertain() -> None:
        # NA 多 FAIL 少但非 HEADING_STEP → uncertain(资源噪声启发式只认 HEADING_STEP)
        a = _agg(source="CONSTRAINT", fail_count=1, na_count=3, category=AuditCategory.PROHIBITION)
        out = annotate_aggregate_noise([a])
        assert out[0].noise_label == "uncertain"
