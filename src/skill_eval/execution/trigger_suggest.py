"""trigger-accuracy 操作建议:据触发结果给出「怎么改 description」的可执行建议。

两路:
- ``build_rule_suggestions``:纯规则、永远生成(零成本、确定性)。按 per-probe 结果
  聚合成 recall(漏召)/ false_trigger(误触)/ unresolved(没判成)/ all_good 四类,
  失败时附上 probe 的 prompt 原文,让用户直接看到 description 没够到 / 过宽的具体措辞。
- ``build_llm_suggestion``(同文件,见下):有失败 probe 时自动调一次 LLM 起草修订
  description 草案。

口径:诊断、不变成 gate——文案只给「改 description 的方向」,不下 PASS/FAIL 判决
(同 handbook 15.6 / 遵循性「宁少报不乱报」哲学)。
"""

from __future__ import annotations

from skill_eval.models.trigger_report import Suggestion
from skill_eval.utils import call_claude


def _bullets(probes: list[dict]) -> str:
    """拼「  · probe-<id>:「<prompt>」」列表(message 里列出失败 probe 的具体措辞)。"""
    return "".join(f"\n  · probe-{p['id']}:「{p['prompt']}」" for p in probes)


def build_rule_suggestions(probes: list[dict]) -> list[Suggestion]:
    """按 per-probe 结果生成规则建议(0-4 条,永远生成)。

    ``probes`` 元素是 ``trigger_runner.run()`` 返回的 per-probe dict,须含
    ``id`` / ``expect_trigger`` / ``prompt`` / ``verdict``。``verdict`` 取
    ``PASS`` / ``FAIL`` / ``UNRESOLVED``。
    """
    failed_recall = [p for p in probes if p["expect_trigger"] and p["verdict"] == "FAIL"]
    failed_ftr = [p for p in probes if not p["expect_trigger"] and p["verdict"] == "FAIL"]
    unresolved = [p for p in probes if p["verdict"] == "UNRESOLVED"]

    out: list[Suggestion] = []

    if failed_recall:
        out.append(Suggestion(
            category="recall", source="rule", probe_ids=[p["id"] for p in failed_recall],
            message=(
                f"漏召:{len(failed_recall)} 条该触发的任务 cc 没触发,说明 description "
                f"没够到这类措辞。考虑在 description 里补与这些请求匹配的触发关键词 / "
                f"动作动词:{_bullets(failed_recall)}"
            ),
        ))

    if failed_ftr:
        out.append(Suggestion(
            category="false_trigger", source="rule", probe_ids=[p["id"] for p in failed_ftr],
            message=(
                f"误触:{len(failed_ftr)} 条不该触发的任务 cc 却触发了,说明 description "
                f"过宽。考虑收窄 scope / 加边界排除语(明确写「不处理这类」):{_bullets(failed_ftr)}"
            ),
        ))

    if unresolved:
        out.append(Suggestion(
            category="unresolved", source="rule", probe_ids=[p["id"] for p in unresolved],
            message=(
                f"{len(unresolved)} 条探题没判成(cc 异常 / 限流),结果可能不完整。"
                f"建议加大 --runs 重跑确认,别据未判成的条下结论。"
            ),
        ))

    if not failed_recall and not failed_ftr and not unresolved:
        out.append(Suggestion(
            category="all_good", source="rule", probe_ids=[],
            message=(
                "触发准确度良好:该触发的都触发、不该的没误触、无未判成。"
                "description 的触发边界当前够精准,无需改动。"
            ),
        ))

    return out


def _classify_call_failure(exc: Exception) -> str:
    """把 call_claude 的异常分类成给用户看的失败原因(限流/超时/cc 非零退出)。

    ``_spawn_claude`` 失败时 raise 的 ``RuntimeError`` 消息含原始 stdout/stderr 片段;
    智谱后端限流(过载)常含 529 / overloaded / 访问量过大,超时消息含 "timed out"。
    """
    msg = str(exc)
    if "timed out" in msg:
        return "超时"
    low = msg.lower()
    if any(k in low for k in ("529", "overloaded", "rate limit", "访问量过大", "限流", "too many")):
        return "疑似限流(cc 非零退出)"
    return "cc 非零退出"


def build_llm_suggestion(
    description: str,
    failed_should_trigger: list[dict],
    failed_should_not: list[dict],
    model: str | None = None,
    timeout: int = 600,
    retries: int = 2,
) -> tuple[Suggestion | None, dict, str | None]:
    """有失败 probe 时,调 LLM 起草修订 description 草案(限流自动重试)。

    - 全 PASS(无失败)→ ``(None, {}, None)``,**不调** LLM(零成本)。
    - 成功 → ``(Suggestion(category=llm_rewrite), usage, None)``。
    - 重试耗尽 → ``(None, {}, reason)``:``reason`` 描述失败类目(限流/超时/空 result/cc 非
      零退出),供 CLI 打印让用户判断是瞬态还是真问题。规则建议不受影响(调用方照给)。

    限流是瞬态:trigger 本体跑完 10 万 token 后紧接着调,常撞智谱后端限流窗口 → cc 非零
    退出。故默认重试 2 次(初次 + 2);每次失败本身耗时(超时/退出要几秒),充作重试间隔。
    ``failed_*`` 是 per-probe dict 列表(含 ``id`` / ``prompt``)。
    """
    if not failed_should_trigger and not failed_should_not:
        return None, {}, None

    should_lines = "\n".join(f"  - {p['prompt']}" for p in failed_should_trigger) or "  (无)"
    should_not_lines = "\n".join(f"  - {p['prompt']}" for p in failed_should_not) or "  (无)"
    prompt = (
        "你在帮一个 Claude Code skill 作者改进 skill frontmatter 的 description 字段"
        "(cc 据这个 description 决定是否自动触发本 skill)。\n\n"
        f"当前 description:\n\"\"\"\n{description}\n\"\"\"\n\n"
        "问题:\n"
        "- 这些任务本 skill【应该】处理,但 cc 没触发它(description 没够到):\n"
        f"{should_lines}\n"
        "- 这些任务本 skill【不应该】处理,但 cc 误触发了(description 过宽):\n"
        f"{should_not_lines}\n\n"
        "请起草修订后的 description:直接给可用的 description 文本(不要 markdown 标题、"
        "不要「以下是」之类开场),让它能触发上面「应该处理」的任务、不触发「不应该处理」"
        "的任务。然后用一行「理由:」简述改了什么(≤2 句)。"
    )

    usage: dict = {}
    last_reason: str | None = None
    for _attempt in range(retries + 1):  # 初次 + retries 次重试
        try:
            resp = call_claude(prompt, model=model, timeout=timeout, max_turns=1, on_usage=usage.update)
        except Exception as exc:
            last_reason = _classify_call_failure(exc)
            continue
        resp = (resp or "").strip()
        if not resp:
            last_reason = "空 result(cc 返回空文本,可能拒答/限流)"
            continue
        failed_ids = [p["id"] for p in failed_should_trigger] + [p["id"] for p in failed_should_not]
        return Suggestion(
            category="llm_rewrite", source="llm", probe_ids=failed_ids, message=resp,
        ), usage, None
    return None, usage, last_reason
