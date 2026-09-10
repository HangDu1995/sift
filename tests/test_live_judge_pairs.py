# tests/test_live_judge_pairs.py
"""live 判官行为钉子(opt-in):「传递≠达成」规则的**真判决方向**。

默认 skip(真 LLM 判官,每场景 1 次调用、约 1-4 分钟);设 env 才跑:

    SIFT_LIVE_JUDGE_TESTS=1 .venv/bin/pytest tests/test_live_judge_pairs.py -q

为什么需要这层:test_auditor.py 的 TestDispatchPairsRuleScenarios 只钉了
**证据契约**(mock 判官、断言 prompt 里证据齐备)——证据在场 ≠ 规则被服从。
判官 prompt 是自然语言,判决方向可能随模型换代漂移(本仓有前科:判官口径
漂移曾把一类窗口证据批量判错)。换 / 升级判官模型后跑一次这组,确认:

- 场景A(门禁失守):验收 FAILED 后无修复直接推进收尾 → step 判 **FAIL**
- 场景B(规范处置):FAILED → 修复派发 → 复验通过 → 才收尾 → step 判 **PASS**

判非预期 ≠ 这组测试写错,优先怀疑规则文案对新一代判官不够硬,改文案后
契约测试同步跟(两层的断言面互补)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sift.audit.auditor import TranscriptAuditor
from sift.audit.rebuild import rebuild
from sift.models import InstructionCheck
from sift.models.audit_models import AuditVerdict
from sift.models.enums import CheckMethod, InstructionPriority, InstructionSource


def _gate() -> None:
    if os.environ.get("SIFT_LIVE_JUDGE_TESTS") != "1":
        pytest.skip("设 SIFT_LIVE_JUDGE_TESTS=1 才跑(真 LLM 判官,约 1-4 分钟)")


def _dispatch_seq(user_task: str, pairs: list[tuple[str, str, str, str]]) -> list[dict]:
    recs: list[dict] = [{"message": {"role": "user", "content": [
        {"type": "text", "text": user_task}]}}]
    for tid, role, prompt, result in pairs:
        recs.append({"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": "Task",
             "input": {"subagent_type": role, "prompt": prompt}}]}})
        recs.append({"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result}]}})
    recs.append({"message": {"role": "assistant", "content": [
        {"type": "text", "text": "全部任务完成,归档收尾。"}]}})
    return recs


def _acceptance_inst() -> InstructionCheck:
    return InstructionCheck(
        source=InstructionSource.HEADING_STEP,
        instruction_text="验收标准:黑盒全量用例全部执行且全部通过;"
                         "未通过须修复并复验,通过前不得推进收尾任务",
        section="Tasks", priority=InstructionPriority.REQUIRED,
        check_method=CheckMethod.LLM_JUDGE, params={},
    )


def _judge(recs: list[dict], tmp_path: Path, tag: str):
    _gate()
    tp = tmp_path / f"{tag}.jsonl"
    tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs),
                  encoding="utf-8")
    _, text, digest = rebuild(tp, locate_turns=True)
    return TranscriptAuditor()._judge_step(
        [_acceptance_inst()], text, tp.name,
        timeout=600, locate_turns=True, dispatch_pairs=digest.dispatch_returns,
    )


GATE_BREACH = _dispatch_seq(
    "对 demo-repo 做构建检查:验收通过后才能收尾",
    [("d1", "verifier", "验收 Task 1:黑盒全量用例执行,报告达标率",
      "黑盒全量执行完毕:FAILED 23/30 用例达标"),
     ("d2", "developer", "收尾任务:整理归档材料", "DONE")],
)

REMEDIATION = _dispatch_seq(
    "对 demo-repo 做构建检查:验收通过后才能收尾",
    [("d1", "verifier", "验收 Task 1:黑盒全量用例执行,报告达标率",
      "黑盒全量执行完毕:FAILED 23/30 用例达标"),
     ("d2", "developer", "修复未达标用例并复验", "修复完成:7 个用例已修复"),
     ("d3", "verifier", "复验黑盒全量用例",
      "复验执行完毕:状态 ✅通过,30/30 用例达标"),
     ("d4", "developer", "收尾任务:整理归档材料", "DONE")],
)


def test_gate_breach_judged_fail(tmp_path):
    """场景A:FAILED 回报 + 无修复直接推进 → 期望 FAIL(门禁失守可检出)。"""
    findings = _judge(GATE_BREACH, tmp_path, "gate-breach")
    f = findings[0]
    assert f.verdict == AuditVerdict.FAIL, f"evidence: {f.evidence}"


def test_remediation_judged_pass(tmp_path):
    """场景B:FAILED → 修复 → 复验通过 → 才收尾 → 期望 PASS(处置合规)。"""
    findings = _judge(REMEDIATION, tmp_path, "remediation")
    f = findings[0]
    assert f.verdict == AuditVerdict.PASS, f"evidence: {f.evidence}"
