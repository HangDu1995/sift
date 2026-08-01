"""trigger-accuracy 评测编排器:per-probe 沙箱 spawn → 检测 skill 是否自然触发 → 聚合。

trigger-accuracy 测的是 cc 会不会**仅凭 skill 的 description** 自然调用本 skill(不评
输出对错)。本模块是 Task 4 产物——把 Task 1-3 搭好的零件(TriggerProbe 模型、
ClaudeRunner.run_trigger spawn 路径)编排成端到端流水线:

  对每条 TriggerProbe:
    1. 建隔离沙箱 ``out_base/probe-<id>/run-<r>/``,把被测 skill 拷进
       ``<sandbox>/.claude/skills/<name>/`` —— cc 在 sandbox 启动后会扫此目录、按
       description 决定是否触发(详见 CLAUDE.md「cc -p 自动触发 skill」记忆条)。
    2. 干净 HOME(``tempfile.mkdtemp``)压掉用户全局 plugin,只留自动发现机制 +
       调用方传入的 auth/身份(``claude_cli_env``),确保触发**只由 sandbox 里的
       本 skill description** 决定、不受用户环境噪声干扰。
    3. ``run_trigger`` spawn(prompt 原样、不灌 body、不关 slash)→ stdout(流式 json)
       写 ``<sandbox>/transcript.jsonl``。
    4. 复用 ``audit/slice.invoked_skills`` 检出 transcript 里出现的 skill 名,判定本
       被测 skill 是否被触发(``self.name in invoked_skills``)。
  多 run 聚合成 per-probe ``trigger_rate``(fired 次数 / runs)、0.5 阈值二值化 →
  PASS/FAIL;spawn 异常 → UNRESOLVED(不进 recall/false_trigger 分母,避免假 0)。
  全方向探题聚合出 recall(should-trigger 方向均值)+ false_trigger_rate(should-not
  方向均值);空方向 → None(N/A,不是 0.0)。

并发:``workers`` 仅占位(API 稳定预留),v1 **串行**跑。trigger spawn 涉及进程组、
临时 HOME、transcript 写盘,线程池的正确隔离成本高、收益小(每 probe 独立 cc 子进程
本就串行瓶颈在 cc 自身);待真出现「批量探题跑太慢」痛点再上线程池,先保正确简单。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from skill_eval.audit.slice import invoked_skills
from skill_eval.execution.cli_runner import ClaudeRunner
from skill_eval.models.eval_models import TriggerProbe
from skill_eval.parser import parse_skill_md
from skill_eval.utils import claude_cli_env


# ---------------------------------------------------------------------------
# 纯函数:单次 / 聚合判定
# ---------------------------------------------------------------------------

def probe_verdict(expect_trigger: bool, fired: bool) -> str:
    """单次 fired → PASS/FAIL。

    - 该触发 & 触发了 / 不该触发 & 没触发 → PASS
    - 该触发 & 没触发(漏召)/ 不该触发 & 触发了(误触发)→ FAIL
    """
    if expect_trigger:
        return "PASS" if fired else "FAIL"
    return "PASS" if not fired else "FAIL"  # should-not:触发了 = 误触发 = FAIL


def _binary_verdict(expect_trigger: bool, trigger_rate: float, threshold: float = 0.5) -> str:
    """多 run 聚合后的 trigger_rate 经阈值二值化 → probe_verdict。

    默认 0.5:fired 次数过半即判 fired。严格阈值由调用方按需覆盖。
    """
    fired = trigger_rate >= threshold
    return probe_verdict(expect_trigger, fired)


def aggregate_rates(probes: list[dict]) -> dict:
    """按方向聚合 trigger_rate → recall / false_trigger_rate。

    ``probes`` 元素形如 ``{"expect_trigger": bool, "trigger_rate": float}``;
    **UNRESOLVED 探题不传进来**(调用方先过滤,避免它们的假 0 拉低 recall)。

    - ``recall``:should-trigger 方向 trigger_rate 均值(越高越好)。
    - ``false_trigger_rate``:should-not 方向 trigger_rate 均值(越低越好)。
    - 某方向空(无该类探题)→ 对应率 None(N/A,报告层显「-」,不是假 0.0)。
    """
    should = [p["trigger_rate"] for p in probes if p["expect_trigger"]]
    should_not = [p["trigger_rate"] for p in probes if not p["expect_trigger"]]
    recall = sum(should) / len(should) if should else None
    ftr = sum(should_not) / len(should_not) if should_not else None
    return {
        "recall": recall,
        "false_trigger_rate": ftr,
        "n_should_trigger": len(should),
        "n_should_not": len(should_not),
    }


# ---------------------------------------------------------------------------
# TriggerRunner — 沙箱/spawn/检测/聚合 编排
# ---------------------------------------------------------------------------

class TriggerRunner:
    """trigger-accuracy 探题编排器。

    每个 probe 在独立沙箱(含 .claude/skills/<name>/)跑、干净 HOME 隔离全局 plugin、
    ``run_trigger`` spawn 后写 transcript、复用 ``invoked_skills`` 检测是否触发。
    """

    def __init__(
        self,
        skill_path: Path | str,
        model: str | None = None,
        timeout: int = 120,
        max_turns: int = 10,
        runs: int = 1,
        workers: int = 4,
    ):
        """
        Args:
            skill_path: 被测 skill 目录(含 SKILL.md)。
            model / timeout / max_turns: 透传 run_trigger。
            runs: 每个 probe 重复跑几次(trigger 带随机性,多次采样聚合成 trigger_rate)。
            workers: 占位(API 稳定预留);v1 串行跑,本参数当前不生效。
        """
        self.skill_path = Path(skill_path)
        # parse_skill_md 返 (name, description, content, frontmatter, body)
        self.name, self.description, *_ = parse_skill_md(self.skill_path)
        self.runner = ClaudeRunner()
        self.model = model
        self.timeout = timeout
        self.max_turns = max_turns
        self.runs = runs
        self.workers = workers  # v1 不用(串行),占位待后续线程池

    def _sandbox_with_skill(self, base: Path, probe_id: int, run: int) -> Path:
        """建 ``base/probe-<id>/run-<run>/`` 沙箱,把被测 skill 拷进 ``.claude/skills/<name>/``。

        跳过 skill 目录里自带的 ``.claude``(避免 skill 自带 project plugin 污染隔离);
        其余文件/子目录(含 references/scripts 等触发后 cc 会读的附件)整树拷过去。
        """
        sandbox = base / f"probe-{probe_id}" / f"run-{run}"
        dst = sandbox / ".claude" / "skills" / self.name
        dst.mkdir(parents=True, exist_ok=True)
        for item in self.skill_path.iterdir():
            if item.name == ".claude":
                continue
            target = dst / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        return sandbox

    def _run_probe_once(self, probe: TriggerProbe, out_base: Path, run: int) -> tuple[bool, str, int]:
        """单 probe 单 run:建沙箱 → 干净 HOME → spawn → 写 transcript → 检测 fired。

        Returns:
            ``(fired, transcript_path, total_tokens, exit_code)``。``fired`` = 被测 skill 名
            (frontmatter name)出现在 ``invoked_skills`` 检出列表里。``exit_code`` 透传给
            ``run()`` 判非零退出(cc 撞限流 / 异常退出常 exit 1 + 错误打 stdout、stderr 空,
            此时 ``run_trigger`` 不 raise,须据此判 UNRESOLVED,见 ``run()``)。
        """
        sandbox = self._sandbox_with_skill(out_base, probe.id, run)
        # 干净 HOME 压全局 plugin:claude_cli_env 补身份/auth,再覆 HOME 为临时空目录,
        # 让 cc 只看 sandbox/.claude/skills/(自动发现) + 不加载用户 ~/.claude 全局插件。
        env = claude_cli_env()
        env["HOME"] = tempfile.mkdtemp(prefix="trigger-home-")
        out = self.runner.run_trigger(
            prompt=probe.prompt,
            cwd=sandbox,
            env=env,
            model=self.model,
            timeout=self.timeout,
            max_turns=self.max_turns,
        )
        transcript = sandbox / "transcript.jsonl"
        transcript.write_text(out.stdout, encoding="utf-8")
        fired = self.name in invoked_skills(transcript)
        return fired, str(transcript), out.total_tokens, out.exit_code

    def run(self, probes: list[TriggerProbe], out_base: Path) -> dict:
        """跑全部探题、聚合 trigger 率。

        两种异常 → probe **任一** run 命中即整 probe 计 UNRESOLVED、不进聚合分母
        (保守:宁少报不乱报,避免假 0 / 异常污染 recall):

        - spawn 抛异常(ClaudeRunnerError / timeout / CLI 缺失…);
        - cc **非零退出且没触发**(``exit_code != 0 and not fired``)——cc 后端不可用 /
          限流时实测 exit 1 + 错误打 stdout、stderr 空,``run_trigger`` 不 raise 而返正常
          RunOutput;若不判会把它当「正常没触发」→ should-trigger 假 FAIL / should-not
          假 PASS,静默污染双率。skill 先触发后 cc 崩(``fired=True``)则照算,不丢 fire。
          UNRESOLVED probe 的 ``trigger_rate`` 置 ``None``(没判成、不显假数值率,
          避免把「没跑出来」误读成「触发率 0」)。
          返回结构::

            {"probes": [per-probe dict], "recall", "false_trigger_rate",
             "n_should_trigger", "n_should_not", "unresolved"}
        """
        results: list[dict] = []
        for probe in probes:
            fired_count = 0
            unresolved = False
            tokens = 0
            transcript_path = ""
            for r in range(self.runs):
                try:
                    fired, path, tk, exit_code = self._run_probe_once(probe, out_base, r)
                    fired_count += int(fired)
                    tokens += tk
                    if not transcript_path:
                        transcript_path = path   # 人审入口:首个成功 run 的 transcript
                    if not fired and exit_code != 0:
                        unresolved = True   # cc 异常退出且没触发 → 无法判定,计 UNRESOLVED
                except Exception:
                    unresolved = True
            trigger_rate = fired_count / self.runs if self.runs else 0.0
            verdict = "UNRESOLVED" if unresolved else _binary_verdict(
                probe.expect_trigger, trigger_rate
            )
            results.append({
                "id": probe.id,
                "expect_trigger": probe.expect_trigger,
                "trigger_rate": None if unresolved else trigger_rate,
                "verdict": verdict,
                "unresolved": unresolved,
                "tokens": tokens,
                "transcript_path": transcript_path,
            })
        resolved = [r for r in results if not r["unresolved"]]
        agg = aggregate_rates([
            {"expect_trigger": r["expect_trigger"], "trigger_rate": r["trigger_rate"]}
            for r in resolved
        ])
        agg["unresolved"] = sum(1 for r in results if r["unresolved"])
        return {"probes": results, **agg}
