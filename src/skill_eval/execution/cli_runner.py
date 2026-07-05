"""Claude CLI subprocess runner."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from skill_eval.errors import ClaudeRunnerError
from skill_eval.models import RunOutput
from skill_eval.parser import parse_skill_md


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


class ClaudeRunner:
    def run(
        self,
        prompt: str,
        skill_path: Path | None = None,
        model: str | None = None,
        timeout: int = 120,
        cwd: Path | None = None,
        max_turns: int = 10,
    ) -> RunOutput:
        project_root = cwd or find_project_root()

        try:
            skill_injected_prompt = prompt
            if skill_path:
                name, description, content, _, skill_body = parse_skill_md(skill_path)
                skill_injected_prompt = _build_non_interactive_prompt(prompt, name, skill_body)

            cmd = ["claude", "-p", skill_injected_prompt, "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions",
                    "--max-turns", str(max_turns)]
            if not skill_path:
                cmd.append("--disable-slash-commands")
            if model:
                cmd.extend(["--model", model])

            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            start = time.time()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(project_root),
                    env=env,
                )
                try:
                    stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
                    stdout = stdout_bytes.decode("utf-8", errors="replace")
                    if stdout.strip():
                        transcript, total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace = self._parse_stream_json(stdout)
                        if transcript or result_text or total_tokens > 0:
                            duration_ms = int((time.time() - start) * 1000)
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
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                returncode = proc.returncode
            except ClaudeRunnerError:
                raise
            except FileNotFoundError:
                raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")
            duration_ms = int((time.time() - start) * 1000)

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
                total_tokens = event.get("total_tokens", total_tokens) or total_tokens
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
