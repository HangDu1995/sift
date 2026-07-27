"""tests/test_audit_parallel.py — 三个 LLM 判定器并发跑(ThreadPoolExecutor max_workers=3)。

**独立文件**(不带 test_auditor.py 的 `_no_llm_retry_sleep` autouse fixture):那个 fixture
patch 的是 `skill_eval.audit.auditor.time.sleep`,而 `time` 是全局模块对象 → 等于全局 patch
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
from pathlib import Path

from skill_eval.audit.auditor import TranscriptAuditor
from skill_eval.models.audit_models import AuditCategory


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


BODY = (
    "use Bash\n\n"
    "NEVER delete production data\n\n"
    "1. Analyze the input format carefully\n\n"
    "## 输出格式\n\n"
    "```\nEXPECTED_TOKEN\n```\n"
)


def _ok_json() -> str:
    """覆盖 0..19 的通用判定 JSON(三解析器各只读自己关心的键)。"""
    return json.dumps([
        {"index": i, "applicable": False, "violated": False,
         "verdict": "pass", "evidence": "ok"}
        for i in range(20)
    ])


class TestParallelJudges:
    def test_judges_actually_run_concurrently(self, tmp_path, monkeypatch) -> None:
        """三个判定器的 call_claude 至少 2 个同时 in-flight → 并发真发生(非串行伪装)。

        用 in-flight 计数器测"同时重叠"(比数线程 id 稳:ThreadPoolExecutor 惰性建线程,
        线程数波动;但 ≥2 个调用同时 in-flight 即证并发)。fake 里真 sleep 0.1s 拉长窗口
        (本文件无 time.sleep patch,sleep 生效)。
        """
        inflight = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def fake(prompt, *a, **k):
            with lock:
                inflight[0] += 1
                max_concurrent[0] = max(max_concurrent[0], inflight[0])
            time.sleep(0.1)
            with lock:
                inflight[0] -= 1
            return _ok_json()

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        tp = _write_jsonl(
            tmp_path / "only_user.jsonl",
            [{"message": {"role": "user", "content": "hello"}}],
        )
        TranscriptAuditor().audit("s", BODY, [tp])
        assert max_concurrent[0] >= 2  # ≥2 个 call_claude 同时在飞 → 并发发生

    def test_parallel_audit_findings_complete(self, tmp_path, monkeypatch) -> None:
        """并发下四类 findings 仍齐全(无竞态丢结果)。"""
        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", lambda *a, **k: _ok_json())
        tp = _write_jsonl(
            tmp_path / "u.jsonl", [{"message": {"role": "user", "content": "x"}}]
        )
        report = TranscriptAuditor().audit("s", BODY, [tp])
        cats = {f.category for f in report.findings}
        assert cats == {  # 四类都产了,并发没丢
            AuditCategory.CONDITIONAL, AuditCategory.PROHIBITION,
            AuditCategory.STEP, AuditCategory.OUTPUT,
        }
        assert len(report.findings) >= 4

    def test_within_category_batches_run_concurrently(self, tmp_path, monkeypatch) -> None:
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
            with lock:
                inflight[0] += 1
                max_concurrent[0] = max(max_concurrent[0], inflight[0])
            time.sleep(0.1)
            with lock:
                inflight[0] -= 1
            return json.dumps([
                {"index": i, "verdict": "pass", "evidence": "ok"} for i in range(30)
            ])

        monkeypatch.setattr("skill_eval.audit.auditor.call_claude", fake)
        body = "".join(f"{i}. Perform step {i} on the input carefully\n\n" for i in range(1, 26))
        tp = _write_jsonl(
            tmp_path / "u.jsonl", [{"message": {"role": "user", "content": "x"}}],
        )
        TranscriptAuditor().audit("s", body, [tp])
        assert max_concurrent[0] >= 2  # 同类多批并发 → ≥2(旧实现串行 ==1,此断言失败)
