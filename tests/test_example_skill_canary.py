"""log-error-analyzer example skill 的金标准 canary 测试。

合成 transcript(transcripts/synthetic-violation.jsonl)植入 3 个已知违规:
- 没读 references/error-codes.md 就分类(该读没读)
- 编造了 error-codes.md 里不存在的码 E-FAKE-999(违反 NEVER fabricate)
- 没写 error-report.json,只口头总结(该做没做)
另含做对的(读日志、引行号)与场景不触发的(无安全事件 → security-policy 不读)。

真 LLM 跑 audit 应抓到这 3 个违规。这是 audit(路径2)**有效性下界**的回归夹具:
工具改动后 `RUN_SLOW=1 pytest tests/test_example_skill_canary.py` 复验判定逻辑没退化。

默认 skip(真调 LLM 慢、且有方差);断言宽松(关键词出现即可,不绑精确 verdict 数)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from skill_eval.parser import parse_skill_md
from skill_eval.audit.auditor import TranscriptAuditor

SKILL_DIR = Path(__file__).parent.parent / "fixtures" / "example-skills" / "log-error-analyzer"
TRANSCRIPT = SKILL_DIR / "transcripts" / "synthetic-violation.jsonl"

_SLOW = pytest.mark.skipif(
    os.environ.get("RUN_SLOW") != "1",
    reason="真调 LLM 的 canary 慢测;RUN_SLOW=1 pytest 启用",
)


@_SLOW
class TestExampleSkillCanary:
    """真 LLM 在干净输入下抓植入违规(canary 回归)。"""

    def test_real_llm_catches_planted_violations(self) -> None:
        name, _desc, _content, _front, body = parse_skill_md(SKILL_DIR)
        report = TranscriptAuditor().audit(name, body, [TRANSCRIPT])

        fails = [f for f in report.findings if f.verdict.value == "FAIL"]
        fail_text = " ".join(
            f"{f.instruction_text} {f.evidence}" for f in fails
        ).lower()

        # 3 个植入违规都应在 FAIL 体现(宽松:关键词出现,容忍 LLM 方差)
        assert "error-codes" in fail_text or "references" in fail_text, (
            f"应抓到'没读 error-codes.md'违规。FAIL 文本:\n{fail_text}"
        )
        assert "fabricat" in fail_text or "e-fake" in fail_text, (
            f"应抓到'编造错误码 E-FAKE-999'违规。FAIL 文本:\n{fail_text}"
        )
        assert "report" in fail_text or "write" in fail_text, (
            f"应抓到'没写 error-report.json'违规。FAIL 文本:\n{fail_text}"
        )
        # 有 PASS(证明工具在区分做对/做错,不是无差别 FAIL)
        assert any(f.verdict.value == "PASS" for f in report.findings), (
            f"应至少一条 PASS(读日志/引行号)。findings:\n{report.summary}"
        )
        # summary 与 findings 逐项一致
        assert report.summary["fail"] == len(fails)
