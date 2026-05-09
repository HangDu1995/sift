"""Trigger evaluator — tests if skill description causes correct triggering."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from skill_eval.models import TriggerQuery


def _run_single_query(
    query: str,
    skill_name: str,
    skill_description: str,
    timeout: int,
    project_root: str,
    model: str | None = None,
) -> bool:
    unique_id = uuid.uuid4().hex[:8]
    clean_name = f"{skill_name}-skill-{unique_id}"
    commands_dir = Path(project_root) / ".claude" / "commands"
    command_file = commands_dir / f"{clean_name}.md"

    try:
        commands_dir.mkdir(parents=True, exist_ok=True)
        indented_desc = "\n  ".join(skill_description.split("\n"))
        escaped_desc = skill_description.replace(":", "\\:").replace("#", "\\#")
        command_file.write_text(
            f"---\ndescription: |\n  {escaped_desc}\n---\n\n# {skill_name}\n\n{skill_description}\n",
            encoding="utf-8",
        )

        cmd = ["claude", "-p", query, "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--dangerously-skip-permissions"]
        if model:
            cmd.extend(["--model", model])

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=project_root, env=env)

        triggered = False
        start_time = time.time()
        buffer = ""
        pending_tool_name = None
        accumulated_json = ""

        try:
            while time.time() - start_time < timeout:
                if process.poll() is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        buffer += remaining.decode("utf-8", errors="replace")
                    break

                ready, _, _ = select.select([process.stdout], [], [], 1.0)
                if not ready:
                    continue

                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "stream_event":
                        se = event.get("event", {})
                        se_type = se.get("type", "")
                        if se_type == "content_block_start":
                            cb = se.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tn = cb.get("name", "")
                                if tn in ("Skill", "Read"):
                                    pending_tool_name = tn
                                    accumulated_json = ""
                                else:
                                    pending_tool_name = None
                            else:
                                pending_tool_name = None
                        elif se_type == "content_block_delta" and pending_tool_name:
                            delta = se.get("delta", {})
                            if delta.get("type") == "input_json_delta":
                                accumulated_json += delta.get("partial_json", "")
                                if clean_name in accumulated_json:
                                    return True
                        elif se_type in ("content_block_stop", "message_stop"):
                            if pending_tool_name:
                                return clean_name in accumulated_json
                            if se_type == "message_stop":
                                return False
                    elif event.get("type") == "assistant":
                        message = event.get("message", {})
                        for ci in message.get("content", []):
                            if ci.get("type") != "tool_use":
                                continue
                            tn = ci.get("name", "")
                            ti = ci.get("input", {})
                            if tn == "Skill" and clean_name in ti.get("skill", ""):
                                triggered = True
                            elif tn == "Read" and clean_name in ti.get("file_path", ""):
                                triggered = True
                            return triggered
                    elif event.get("type") == "result":
                        return triggered
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        return triggered
    finally:
        if command_file.exists():
            command_file.unlink()


class TriggerEvaluator:
    def __init__(
        self,
        skill_name: str,
        skill_description: str,
        num_workers: int = 10,
        timeout: int = 30,
        project_root: Path | None = None,
        runs_per_query: int = 3,
        trigger_threshold: float = 0.5,
        model: str | None = None,
    ):
        self.skill_name = skill_name
        self.skill_description = skill_description
        self.num_workers = num_workers
        self.timeout = timeout
        self.project_root = str(project_root or Path.cwd())
        self.runs_per_query = runs_per_query
        self.trigger_threshold = trigger_threshold
        self.model = model

    def evaluate(self, eval_set: list[TriggerQuery]) -> dict:
        results = []
        with ProcessPoolExecutor(max_workers=self.num_workers) as executor:
            future_to_info = {}
            for item in eval_set:
                for _ in range(self.runs_per_query):
                    future = executor.submit(
                        _run_single_query,
                        item.query,
                        self.skill_name,
                        self.skill_description,
                        self.timeout,
                        self.project_root,
                        self.model,
                    )
                    future_to_info[future] = item

            query_triggers: dict[str, list[bool]] = {}
            query_items: dict[str, TriggerQuery] = {}
            for future in as_completed(future_to_info):
                item = future_to_info[future]
                query_items[item.query] = item
                query_triggers.setdefault(item.query, [])
                try:
                    query_triggers[item.query].append(future.result())
                except Exception as exc:
                    query_triggers[item.query].append(False)
                    pass

        for query, triggers in query_triggers.items():
            item = query_items[query]
            trigger_rate = sum(triggers) / len(triggers)
            did_pass = (trigger_rate >= self.trigger_threshold) == item.should_trigger
            results.append({
                "query": query,
                "should_trigger": item.should_trigger,
                "trigger_rate": trigger_rate,
                "triggers": sum(triggers),
                "runs": len(triggers),
                "pass": did_pass,
            })

        passed = sum(1 for r in results if r["pass"])
        total = len(results)
        return {
            "skill_name": self.skill_name,
            "description": self.skill_description,
            "results": results,
            "summary": {"total": total, "passed": passed, "failed": total - passed},
        }
