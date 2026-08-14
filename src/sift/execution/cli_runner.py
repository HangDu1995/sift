"""Claude CLI subprocess runner."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from sift.errors import ClaudeRunnerError
from sift.models import RunOutput
from sift.parser import parse_skill_md
from sift.utils import claude_cli_env


def find_project_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


def _build_non_interactive_prompt(prompt: str, skill_name: str, skill_body: str) -> str:
    """构造 with_skill 模式的注入 prompt:skill body + non_interactive 指示 + 原 prompt。

    non_interactive 指示 Claude 自动跑完 skill 流程不卡住,但不诱导捏造数据(P1-4:
    此前 'generate synthetic test data' 与 skill 的反捏造指令冲突,诱导 Claude 违规
    -> compliance 假红灯)。改为缺数据时报告缺失 + 用真实可得数据继续,不捏造。
    """
    return (
        f"You have the following Skill loaded. Follow its instructions when handling the user's request.\n\n"
        f"<skill name=\"{skill_name}\">\n"
        f"{skill_body}\n"
        f"</skill>\n\n"
        f"<non_interactive_mode>\n"
        f"This is a NON-INTERACTIVE automated evaluation.\n"
        f"- Execute ALL phases/stages of the skill end-to-end. DO NOT skip steps.\n"
        f"- When the skill asks for user input (path, choice, confirmation): "
        f"SIMULATE a reasonable user response and continue immediately.\n"
        f"- For file paths: explore the working directory and use what you find.\n"
        f"- For choices: state your assumption and proceed with the recommended option.\n"
        f"- For data input: use real files in the working directory. If required data is "
        f"genuinely missing, state what is missing and proceed with what is available — "
        f"do NOT fabricate or invent data.\n"
        f"- NEVER output questions or wait — always provide the simulated answer inline and continue.\n"
        f"</non_interactive_mode>\n\n"
        f"---\n\n"
        f"{prompt}"
    )


def build_live_prompt(task: str, skill_path: Path | None) -> str:
    """构造路径1 真任务注入 prompt:skill body + 非交互前言 + task。

    cc/opencode 两执行器共用 → 保证同一份 prompt(执行器无关)。``skill_path`` 为 None
    (无 skill 注入)时原样返回 task。抽自 ClaudeRunner.run 内联块,供 OpenCodeRunner 复用。
    """
    if skill_path is None:
        return task
    name, _description, _content, _, skill_body = parse_skill_md(skill_path)
    return _build_non_interactive_prompt(task, name, skill_body)


def _progress_from_event(line: str) -> str | None:
    """stream-json 一行 → 进度字符串(供 run 流式 on_progress 实时吐),无足轻重返 None。

    cc ``--output-format stream-json`` 逐行出事件;run 流式读时每行调此,非 None 即经
    on_progress 吐出,让长 spawn 有可见进度(同 audit 的 on_progress 套路)。
    tool_use 是动作信号(Read/Bash/Write…),result 是完成信号;assistant 散文太吵不报。
    """
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    typ = ev.get("type")
    if typ == "assistant":
        for item in (ev.get("message") or {}).get("content") or []:
            if item.get("type") == "tool_use":
                name = item.get("name", "?")
                inp = item.get("input") or {}
                brief = (inp.get("file_path") or inp.get("command") or inp.get("description")
                         or inp.get("prompt") or (next(iter(inp.values())) if inp else ""))
                brief = str(brief).replace("\n", " ").strip()
                if len(brief) > 80:
                    brief = brief[:77] + "…"
                return f"🔧 {name}: {brief}" if brief else f"🔧 {name}"
        return None
    if typ == "result":
        usage = ev.get("usage") or {}
        toks = ev.get("total_tokens") or ((usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0))
        stop = ev.get("stop_reason") or ev.get("subtype") or ""
        turns = ev.get("num_turns", 0)
        return f"✓ 完成: {stop}, {turns} 轮, {toks} tok"
    return None


class ClaudeRunner:
    def run(
        self,
        prompt: str,
        skill_path: Path | None = None,
        model: str | None = None,
        timeout: int = 120,
        cwd: Path | None = None,
        max_turns: int = 10,
        on_progress: Callable[[str], None] | None = None,
    ) -> RunOutput:
        project_root = cwd or find_project_root()

        try:
            skill_injected_prompt = build_live_prompt(prompt, skill_path)

            cmd = ["claude", "-p", skill_injected_prompt, "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions",
                    "--max-turns", str(max_turns)]
            if not skill_path:
                cmd.append("--disable-slash-commands")
            if model:
                cmd.extend(["--model", model])

            env = claude_cli_env()

            try:
                stdout, returncode, duration_ms, timed_out, stderr = self._spawn(
                    cmd, project_root, env, timeout, on_progress
                )
            except FileNotFoundError:
                raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")

            if timed_out:
                if stdout.strip():
                    transcript, total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace = self._parse_stream_json(stdout)
                    if transcript or result_text or total_tokens > 0:
                        return RunOutput(
                            exit_code=-1,
                            stdout=stdout,
                            duration_ms=duration_ms,
                            total_tokens=total_tokens,
                            transcript=transcript,
                            tool_calls=tool_calls,
                            tool_trace=tool_trace,
                            result_text=result_text,
                            num_turns=num_turns,
                            stop_reason=f"timed_out_after_{timeout}s",
                        )
                raise ClaudeRunnerError(f"claude -p timed out after {timeout}s (killed process tree)")

            transcript, total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace = self._parse_stream_json(stdout)

            has_output = bool(transcript or result_text or total_tokens > 0)

            if returncode != 0 and not has_output:
                raise ClaudeRunnerError(f"claude -p exited with code {returncode}: {stderr[:500]}")

            return RunOutput(
                exit_code=returncode,
                stdout=stdout,
                duration_ms=duration_ms,
                total_tokens=total_tokens,
                transcript=transcript,
                tool_calls=tool_calls,
                tool_trace=tool_trace,
                result_text=result_text,
                num_turns=num_turns,
                stop_reason=stop_reason or (f"exit_{returncode}" if returncode != 0 else ""),
            )
        except ClaudeRunnerError:
            raise
        except FileNotFoundError:
            raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")
        except Exception as exc:
            raise ClaudeRunnerError(f"claude -p failed: {exc}")

    def run_trigger(
        self,
        prompt: str,
        cwd: Path | str,
        env: dict,
        model: str | None = None,
        timeout: int = 120,
        max_turns: int = 10,
        on_progress: Callable[[str], None] | None = None,
    ) -> RunOutput:
        """trigger 模式 spawn:不灌 body(prompt 原样)、不加 --disable-slash-commands(skill 要开启)。

        trigger-accuracy 评测专用:skill 须由调用方放进 ``cwd/.claude/skills/`` 让 cc 按
        description 自动发现(不靠注入)。两个关键约束(本方法的全部意义):① 绝不加
        ``--disable-slash-commands``(``run()`` 在 ``not skill_path`` 时加,trigger 必须避開——
        否则关掉所有 skill);② prompt 原样传 ``claude -p``,不调 build_live_prompt /
        _build_non_interactive_prompt。``env`` 由调用方传入(含 HOME=<temp> 干净隔离全局
        plugin)——本方法不调 claude_cli_env(),隔离归调用方。
        """
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions", "--max-turns", str(max_turns)]
        # 注意:绝不加 --disable-slash-commands(run() 在 not skill_path 时加,trigger 必须避開)
        if model:
            cmd.extend(["--model", model])
        try:
            stdout, returncode, duration_ms, timed_out, stderr = self._spawn(
                cmd, cwd, env, timeout, on_progress)
        except FileNotFoundError:
            raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")
        if timed_out:
            if stdout.strip():
                return self._assemble(stdout, returncode, duration_ms, timed_out, timeout, stderr)
            raise ClaudeRunnerError(f"claude -p timed out after {timeout}s (killed process tree)")
        if returncode != 0 and not stdout.strip():
            raise ClaudeRunnerError(f"claude -p exited with code {returncode}: {stderr[:500]}")
        return self._assemble(stdout, returncode, duration_ms, timed_out, timeout, stderr)

    def run_agent(
        self,
        prompt: str,
        agent_path: Path,
        model: str | None = None,
        timeout: int = 120,
        cwd: Path | None = None,
        max_turns: int = 10,
        on_progress: Callable[[str], None] | None = None,
    ) -> RunOutput:
        """agent 模式 spawn:agent 正文(frontmatter 后 body)经 ``--append-system-prompt``
        注入 cc 的 system 层(prompt 原样),测 agent 绝对质量(无 A/B 对照)。

        区别于 ``run()``(skill body 包 ``<skill>`` 拼 user prompt):agent 用 cc 原生
        system prompt 注入,贴近真实 dispatch 路径。``description`` 是主 agent 的路由线索、
        不进 system prompt(只取 ``parse_skill_md`` 的 body,与 skill 注入只取 skill_body 对称)。
        env 用 ``claude_cli_env()``(同 run,身份补全)。
        """
        project_root = cwd or find_project_root()
        _name, _description, _content, _refs, body = parse_skill_md(agent_path)
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--dangerously-skip-permissions", "--max-turns", str(max_turns),
               "--append-system-prompt", body]
        # prompt 原样传(不调 build_live_prompt——那是 skill body 拼 user prompt);body 进 system 层
        if model:
            cmd.extend(["--model", model])
        env = claude_cli_env()
        try:
            stdout, returncode, duration_ms, timed_out, stderr = self._spawn(
                cmd, project_root, env, timeout, on_progress)
        except FileNotFoundError:
            raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")
        if timed_out:
            if stdout.strip():
                return self._assemble(stdout, returncode, duration_ms, timed_out, timeout, stderr)
            raise ClaudeRunnerError(f"claude -p timed out after {timeout}s (killed process tree)")
        if returncode != 0 and not stdout.strip():
            raise ClaudeRunnerError(f"claude -p exited with code {returncode}: {stderr[:500]}")
        return self._assemble(stdout, returncode, duration_ms, timed_out, timeout, stderr)

    def _assemble(
        self,
        stdout: str,
        returncode: int,
        duration_ms: int,
        timed_out: bool,
        timeout: int,
        stderr: str,
    ) -> RunOutput:
        """parse + 构 RunOutput(DRY run_trigger 两条路径;字段值与 run() 对应路径逐字一致)。

        timed_out 路径镜像 ``run()`` 的 timed_out-with-partial 分支(``exit_code=-1``、
        ``stop_reason=f"timed_out_after_{timeout}s"`` 无 fallback);正常路径镜像 ``run()``
        的正常分支(``exit_code=returncode``、``stop_reason or exit_{code}`` fallback)。
        ``run()`` 自身因两条路径前都有 has-output 守卫(需先 parse 再判),改接 _assemble 会
        双重 parse,故 run() 保持原样、仅 run_trigger() 用本助手。
        """
        transcript, total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace = \
            self._parse_stream_json(stdout)
        if timed_out:
            exit_code = -1
            final_stop = f"timed_out_after_{timeout}s"
        else:
            exit_code = returncode
            final_stop = stop_reason or (f"exit_{returncode}" if returncode != 0 else "")
        return RunOutput(
            exit_code=exit_code,
            stdout=stdout,
            duration_ms=duration_ms,
            total_tokens=total_tokens,
            transcript=transcript,
            tool_calls=tool_calls,
            tool_trace=tool_trace,
            result_text=result_text,
            num_turns=num_turns,
            stop_reason=final_stop,
        )

    def _spawn(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict,
        timeout: int,
        on_progress: Callable[[str], None] | None,
    ) -> tuple[str, int, int, bool, str]:
        """Popen + 流式读 stdout(queue 超时)+ proc.kill()。返 ``(stdout, returncode, duration_ms, timed_out, stderr)``。

        纯 spawn 机制(无 skill 注入 / 无 slash 控制 / 无 parse),``run()`` 与将来的
        ``run_trigger()``(Task 3)共用。``FileNotFoundError``(Popen 找不到 claude)向上抛,
        由调用方转 ``ClaudeRunnerError``。从 run() 原样抽出,行为字节不变。
        """
        start = time.time()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        # 流式读 stdout:cc --output-format stream-json 逐行出事件,边读边解析吐进度
        # (同 audit on_progress),不再 communicate 全缓冲到结束才返回(那让长 spawn
        # 全程无反馈)。独立线程阻塞 readline + 主线程 queue.get(timeout) 实现超时 +
        # 流式(避开 select 对带缓冲 file 对象的对齐坑)。stderr 不流式(cc stderr
        # 通常很小、常空),proc 退出后一次 read。
        line_queue: queue.Queue = queue.Queue()

        def _stdout_reader() -> None:
            for raw in iter(proc.stdout.readline, b""):
                line_queue.put(raw)
            line_queue.put(None)  # EOF 哨兵

        reader = threading.Thread(target=_stdout_reader, daemon=True)
        reader.start()

        stdout_chunks: list[str] = []
        timed_out = False
        while True:
            remaining = timeout - (time.time() - start)
            if remaining <= 0:
                timed_out = True
                break
            try:
                raw = line_queue.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            if raw is None:
                break  # stdout EOF → cc 结束/退出
            line = raw.decode("utf-8", errors="replace")
            stdout_chunks.append(line)
            if on_progress:
                msg = _progress_from_event(line)
                if msg:
                    on_progress(msg)

        if timed_out:
            proc.kill()
        proc.wait()
        try:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
        except Exception:
            stderr_bytes = b""

        stdout = "".join(stdout_chunks)
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode
        duration_ms = int((time.time() - start) * 1000)
        return stdout, returncode, duration_ms, timed_out, stderr

    def _parse_stream_json(self, stdout: str) -> tuple[str, int, dict[str, int], str, int, str, list]:
        from sift.models import ToolCallRecord
        transcript_parts: list[str] = []
        total_tokens = 0
        tool_calls: dict[str, int] = {}
        result_text = ""
        num_turns = 0
        stop_reason = ""
        tool_trace: list[ToolCallRecord] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "result":
                # token 解析:claude CLI 2.x 的 result event 顶层无 total_tokens,
                # token 在 usage 子对象(input_tokens + output_tokens,标准 Anthropic 字段);
                # 老格式若有顶层 total_tokens 则优先(兼容旧版)。
                result_tokens = event.get("total_tokens") or 0
                if not result_tokens:
                    usage = event.get("usage") or {}
                    result_tokens = ((usage.get("input_tokens") or 0)
                                     + (usage.get("output_tokens") or 0))
                if result_tokens:
                    total_tokens = result_tokens
                result_text = event.get("result", "") or result_text
                num_turns = event.get("num_turns", num_turns)
                stop_reason = event.get("stop_reason", stop_reason) or event.get("subtype", "")

            elif event.get("type") == "assistant":
                message = event.get("message", {})
                for content_item in message.get("content", []):
                    if content_item.get("type") == "text":
                        transcript_parts.append(content_item.get("text", ""))
                    elif content_item.get("type") == "tool_use":
                        tool_name = content_item.get("name", "")
                        tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1
                        tool_input = content_item.get("input", {})
                        input_summary = json.dumps(tool_input, ensure_ascii=False)[:200] if tool_input else ""
                        tool_trace.append(ToolCallRecord(
                            name=tool_name, input_summary=input_summary,
                            input=tool_input or None))  # 完整结构化参数,供参数级匹配

        return "\n".join(transcript_parts), total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace
