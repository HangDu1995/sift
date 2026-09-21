"""tests/test_audit_parallel.py — 三个 LLM 判定器并发跑(ThreadPoolExecutor max_workers=3)。

**独立文件**(不带 test_auditor.py 的 `_no_llm_retry_sleep` autouse fixture):那个 fixture
patch 的是 `sift.audit.auditor.time.sleep`,而 `time` 是全局模块对象 → 等于全局 patch
了 `time.sleep`,会把并发测试 fake 里的 sleep 也变 no-op → 观测不到重叠。并发测试需要真
sleep 来拉长 in-flight 窗口,故单独成文件。

max_workers=3 = 类别数 = 实测安全并发(1/2/3 档全 0 失败)。三判定器无共享可变状态(各自
产 findings 列表),on_progress(console.print)线程安全。验证:① 并发真发生(≥2 个 call_claude
同时 in-flight);② 并发下 findings 仍正确完整(四类齐全)——证线程安全无竞态。
"""

from __future__ import annotations

import json
import threading
import time

from sift.audit.auditor import InstructionEngine, TranscriptAuditor
from sift.audit.judge_context import AuditOptions
from sift.models.audit_models import AuditCategory, AuditVerdict

from .helpers import write_jsonl as _write_jsonl

BODY = (
    "use Bash\n\n"
    "NEVER delete production data\n\n"
    "1. Analyze the input format carefully\n\n"
    "## 输出格式\n\n"
    "```\nEXPECTED_TOKEN\n```\n"
)


def _ok_json() -> str:
    """覆盖 0..19 的通用判定 JSON(三解析器各只读自己关心的键)。"""
    return json.dumps(
        [{"index": i, "applicable": False, "violated": False, "verdict": "pass", "evidence": "ok"} for i in range(20)]
    )


class TestParallelJudges:
    @staticmethod
    def test_judges_actually_run_concurrently(tmp_path, monkeypatch) -> None:
        """三个判定器的 call_claude 至少 2 个同时 in-flight → 并发真发生(非串行伪装)。

        用 in-flight 计数器测"同时重叠"(比数线程 id 稳:ThreadPoolExecutor 惰性建线程,
        线程数波动;但 ≥2 个调用同时 in-flight 即证并发)。fake 里真 sleep 0.1s 拉长窗口
        (本文件无 time.sleep patch,sleep 生效)。
        """
        inflight = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def fake(prompt, *a, **k):
            _track_concurrent_delay(inflight, max_concurrent, lock)
            return _ok_json()

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        TranscriptAuditor().audit(skill_name="s", skill_body=BODY, transcripts=[tp], options=AuditOptions())
        assert max_concurrent[0] >= 2  # ≥2 个 call_claude 同时在飞 → 并发发生

    @staticmethod
    def test_parallel_audit_findings_complete(tmp_path, monkeypatch) -> None:
        """并发下四类 findings 仍齐全(无竞态丢结果)。"""
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: _ok_json())
        tp = _write_jsonl(tmp_path / "u.jsonl", [{"message": {"role": "user", "content": "x"}}])
        report = TranscriptAuditor().audit(skill_name="s", skill_body=BODY, transcripts=[tp], options=AuditOptions())
        cats = {f.category for f in report.findings}
        assert cats == {  # 四类都产了,并发没丢
            AuditCategory.CONDITIONAL,
            AuditCategory.PROHIBITION,
            AuditCategory.STEP,
            AuditCategory.OUTPUT,
        }
        assert len(report.findings) >= 4

    @staticmethod
    def test_within_category_batches_run_concurrently(tmp_path, monkeypatch) -> None:
        """单类别多批也并发跑(桶内并发),不只跨类别并发。

        25 步(HEADING_STEP 单桶)→ 3 批(10/10/5)。旧实现每类内批次**串行** →
        max_concurrent==1;共享批池后同类的多批也并发 → ≥2。单类别是关键:排除跨类别
        并发(cond/proh 空桶短路不调 LLM)的干扰,直证「桶内并发」这一新能力。

        这是 audit 提速的核心:推理瓶颈下,把每类内串行的批次铺到有界池,墙钟从
        「最慢类批数 × 单批」降到「~单批」(GLM 端点实测 6 路并发 0 失败)。
        """
        inflight = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def fake(prompt, *a, **k):
            _track_concurrent_delay(inflight, max_concurrent, lock)
            return json.dumps([{"index": i, "verdict": "pass", "evidence": "ok"} for i in range(30)])

        monkeypatch.setattr("sift.audit.auditor.call_claude", fake)
        body = "".join(f"{i}. Perform step {i} on the input carefully\n\n" for i in range(1, 26))
        tp = _write_jsonl(
            tmp_path / "u.jsonl",
            [{"message": {"role": "user", "content": "x"}}],
        )
        TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())
        assert max_concurrent[0] >= 2  # 同类多批并发 → ≥2(旧实现串行 ==1,此断言失败)


class TestExceptionIsolation:
    """单批/单判定器抛未捕获异常时,audit 不崩、降级 UNRESOLVED(不击穿、不翻转真假)。

    ``judge_batch_with_retry`` 只 ``except RuntimeError``;非 RuntimeError(parse_fn 抛、
    代码 bug、call_claude 包装成别的异常类型)会穿透到 ``fut.result()``。两处裸
    ``fut.result()``(``_run_batches_concurrent`` as_completed + audit 主循环跨类)兜底前
    会击穿整个 audit;兜底后单批/单类降级 UNRESOLVED、其余照常(宁少报不乱报 → 可重跑)。
    """

    @staticmethod
    def test_within_category_batch_exception_does_not_crash(tmp_path, monkeypatch) -> None:
        """非 RuntimeError 穿透 ``judge_batch_with_retry`` 的 except → 单批 future 异常。

        兜底前:``fut.result()`` 击穿 ``_run_batches_concurrent`` → audit 崩;
        兜底后:该批跳过(预置 None → UNRESOLVED),audit 不崩、该类全 UNRESOLVED。
        """

        def boom(*a, **k):
            raise ValueError("simulated 非 RuntimeError 穿透")

        monkeypatch.setattr("sift.audit.auditor.call_claude", boom)
        body = "".join(
            f"{i}. Perform step {i} on the input carefully\n\n" for i in range(1, 26)
        )  # 25 条 step → 同类多批(走 _run_batches_concurrent 并发分支)
        tp = _write_jsonl(tmp_path / "u.jsonl", [{"message": {"role": "user", "content": "x"}}])
        report = TranscriptAuditor().audit(skill_name="s", skill_body=body, transcripts=[tp], options=AuditOptions())
        step_findings = [f for f in report.findings if f.category == AuditCategory.STEP]
        assert step_findings, "step 类应有 finding(25 条指令)"
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in step_findings), "穿透的批应降级 UNRESOLVED,不是击穿崩"

    @staticmethod
    def test_judge_exception_does_not_crash(tmp_path, monkeypatch) -> None:
        """某 ``_judge_*`` 整体抛异常(内部 bug)→ audit 主循环兜底:该类每条 UNRESOLVED,其余类正常。

        兜底前:``fut.result()`` 击穿 audit 主循环 → audit 崩;
        兜底后:该类全 UNRESOLVED,其余类照常判定。
        """

        def boom(self, instructions, *a, **k):
            raise RuntimeError("prohibition 判定器内部 bug")

        monkeypatch.setattr(InstructionEngine, "judge_prohibition", boom)
        monkeypatch.setattr("sift.audit.auditor.call_claude", lambda *a, **k: _ok_json())
        tp = _write_jsonl(tmp_path / "u.jsonl", [{"message": {"role": "user", "content": "x"}}])
        report = TranscriptAuditor().audit(skill_name="s", skill_body=BODY, transcripts=[tp], options=AuditOptions())
        proph = [f for f in report.findings if f.category == AuditCategory.PROHIBITION]
        assert proph, "prohibition 类应有 finding(NEVER 指令)"
        assert all(f.verdict == AuditVerdict.UNRESOLVED for f in proph), "判定器异常应降级该类全 UNRESOLVED"
        # 其余类不受牵连(有非 UNRESOLVED 的正常判定)
        non_proph = [f for f in report.findings if f.category != AuditCategory.PROHIBITION]
        assert any(f.verdict != AuditVerdict.UNRESOLVED for f in non_proph), "其余类应正常判定,不被击穿连累"


def _track_concurrent_delay(inflight: list[int], max_concurrent: list[int], lock) -> None:
    """真实等待期间记录同时在飞数量，保留原测试的锁和重叠窗口。"""
    with lock:
        inflight[0] += 1
        max_concurrent[0] = max(max_concurrent[0], inflight[0])
    time.sleep(0.1)
    with lock:
        inflight[0] -= 1
