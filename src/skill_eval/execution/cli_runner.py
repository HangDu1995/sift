"""Claude CLI subprocess runner."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from skill_eval.errors import ClaudeRunnerError
from skill_eval.models import RunOutput
from skill_eval.parser import parse_skill_md
from skill_eval.utils import claude_cli_env


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
        from skill_eval.models import ToolCallRecord
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
                        tool_trace.append(ToolCallRecord(name=tool_name, input_summary=input_summary))

        return "\n".join(transcript_parts), total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace
