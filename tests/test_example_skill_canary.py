"""example skill 的金丝雀 canary 测试(中英简单 + 复杂容忍度)。

简单 skill(log-error-analyzer 英/中):合成 transcript 植入 3 个已知违规(没读
reference / 编造错误码 / 没写报告),真 LLM audit 应都抓到。中英同内容对照,验证
audit 对中文判定不退化(英文 + 中文 extractor 分支都覆盖)。

复杂 skill(release-checklist-zh):受控版 dispatching——故意有代码块(OUTPUT_FORMAT
噪音)与示例列表(示例当步骤噪音),测 audit 在真实复杂 skill 下真违规能否穿透
extractor 噪音被抓。

默认 skip(真调 LLM 慢且有方差);断言宽松(关键词出现即可,不绑精确 verdict 数)。
`RUN_SLOW=1 pytest tests/test_example_skill_canary.py` 复验。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sift.audit.auditor import TranscriptAuditor
from sift.audit.judge_context import AuditOptions
from sift.parser import parse_skill_md

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "example-skills"
# 中英简单 skill 同内容对照
SIMPLE_SKILLS = [
    ("en", _FIXTURES / "log-error-analyzer"),
    ("zh", _FIXTURES / "log-error-analyzer-zh"),
]
COMPLEX_SKILL = _FIXTURES / "release-checklist-zh"

_SLOW = pytest.mark.skipif(
    os.environ.get("RUN_SLOW") != "1",
    reason="真调 LLM 的 canary 慢测;RUN_SLOW=1 pytest 启用",
)


def test_incident_reporter_trigger_probes_load():
    """incident-reporter(trigger 专用 example)trigger_probes 完整性:能加载 + 两方向都有。fast,不调 LLM。"""
    import json

    from sift.models import EvalSet

    es = EvalSet.model_validate(
        json.loads((_FIXTURES / "incident-reporter" / "evals" / "evals.json").read_text(encoding="utf-8"))
    )
    assert es.skill_name == "incident-reporter"
    assert len(es.trigger_probes) == 5
    assert any(p.expect_trigger for p in es.trigger_probes)  # 有 should-trigger
    assert any(not p.expect_trigger for p in es.trigger_probes)  # 有 should-not


@_SLOW
class TestExampleSkillCanary:
    """真 LLM 在干净输入下抓植入违规(canary 回归)。中英对照 + 复杂容忍度。"""

    @staticmethod
    def test_complex_catches_planted_violations() -> None:
        """复杂 skill(受控 dispatching):真违规应穿透 extractor 噪音被抓。

        植入:跑了测试但没查 lint、编造 lint/依赖通过、breaking change 没审批、
        没读 references。extractor 会抽出示例当步骤 + 代码块当 OUTPUT_FORMAT 的噪音,
        但真违规应仍出现在 FAIL。
        """
        transcript = COMPLEX_SKILL / "transcripts" / "synthetic-violation.jsonl"
        name, _desc, _content, _front, body = parse_skill_md(COMPLEX_SKILL)
        report = TranscriptAuditor().audit(
            skill_name=name, skill_body=body, transcripts=[transcript], options=AuditOptions()
        )

        fails = [f for f in report.findings if f.verdict.value == "FAIL"]
        fail_text = " ".join(f"{f.instruction_text} {f.evidence}" for f in fails).lower()

        # 真违规穿透噪音被抓。断言只钉**确定性判定**(不调 LLM、无方差):
        # - 没读 references → match_tool_call 未命中 → FAIL(确定性)
        # - breaking change "直接打 tag" → 稳定被抓
        # 不钉 step 判定(查 lint 等):复杂 skill 28 指令长 prompt 下 LLM step
        # 判定有方差(有时 FAIL 有时 N/A),钉它会 flaky;step 真违规由 ref/tool
        # 确定性 FAIL + 人工 audit 结论覆盖,不进自动 canary。
        assert "references" in fail_text or "version-rules" in fail_text, f"应抓到'没读 references'。FAIL:\n{fail_text}"
        assert "审批" in fail_text or "breaking" in fail_text, f"应抓到'breaking change 没审批'。FAIL:\n{fail_text}"
        # 有 PASS(跑测试/打 tag 做对了,证明不全 FAIL)
        assert any(f.verdict.value == "PASS" for f in report.findings), (
            f"应至少一条 PASS(跑测试/打 tag)。{report.summary}"
        )

    @staticmethod
    def test_opencode_catches_planted_violations() -> None:
        """opencode session 格式:同 3 植入违规,真 LLM audit 应都抓到(格式等价 canary)。

        与 cc canary(test_simple_catches_planted_violations)同内容、同植入,唯一差别
        是 transcript 是 opencode session 格式(.json messages/parts)而非 cc jsonl。
        验证 build_digest_from_opencode → rebuild → TranscriptAuditor 整条链路对
        opencode 格式不退化,抓违规能力与 cc 等价。
        """
        skill_dir = _FIXTURES / "log-error-analyzer-zh"
        transcript = skill_dir / "opencode-sessions" / "synthetic-violation.json"
        name, _desc, _content, _front, body = parse_skill_md(skill_dir)
        report = TranscriptAuditor().audit(
            skill_name=name, skill_body=body, transcripts=[transcript], options=AuditOptions()
        )

        fails = [f for f in report.findings if f.verdict.value == "FAIL"]
        fail_text = " ".join(f"{f.instruction_text} {f.evidence}" for f in fails).lower()
        assert "error-codes" in fail_text or "references" in fail_text, (
            f"应抓到'没读 error-codes.md'。FAIL:\n{fail_text}"
        )
        assert "fabricat" in fail_text or "e-fake" in fail_text, f"应抓到'编造 E-FAKE-999'。FAIL:\n{fail_text}"

    @pytest.mark.parametrize("lang, skill_dir", SIMPLE_SKILLS, ids=["en", "zh"])
    def test_simple_catches_planted_violations(self, lang: str, skill_dir: Path) -> None:
        """中英简单 skill:植入 3 违规,真 LLM 应都抓到(验证中文不退化)。"""
        transcript = skill_dir / "transcripts" / "synthetic-violation.jsonl"
        name, _desc, _content, _front, body = parse_skill_md(skill_dir)
        report = TranscriptAuditor().audit(
            skill_name=name, skill_body=body, transcripts=[transcript], options=AuditOptions()
        )

        fails = [f for f in report.findings if f.verdict.value == "FAIL"]
        fail_text = " ".join(f"{f.instruction_text} {f.evidence}" for f in fails).lower()

        # 3 个植入违规在 FAIL(关键词是英文码/路径,中英 fail_text 都含)
        assert "error-codes" in fail_text or "references" in fail_text, (
            f"[{lang}] 应抓到'没读 error-codes.md'。FAIL:\n{fail_text}"
        )
        assert "fabricat" in fail_text or "e-fake" in fail_text, f"[{lang}] 应抓到'编造 E-FAKE-999'。FAIL:\n{fail_text}"
        assert "report" in fail_text or "write" in fail_text, f"[{lang}] 应抓到'没写报告'。FAIL:\n{fail_text}"
        assert any(f.verdict.value == "PASS" for f in report.findings), f"[{lang}] 应至少一条 PASS。{report.summary}"
        assert report.summary["fail"] == len(fails)
