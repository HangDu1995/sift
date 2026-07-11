"""路径1 真任务模式:不读 evals.json,直接 spawn cc 在真实工作目录跑真实任务。

区别于:
- 路径3(evals.json fixture 剧本 spawn + scoring):任务源是 init 编的剧本 + fixture 数据;
- 路径2(audit 审历史 trace):不 spawn,审已有 trace。

路径1 = 真实工作目录(--cwd 任务材料)+ 真实任务 prompt(--task)+ 被测 skill(--skill)
→ spawn cc 真跑 → dump 真任务 trace → audit 审 skill 遵循性。

设计要点:
- --cwd 拷贝到 output_dir/workspace(隔离考场,cc 不碰原目录);
- skill references/ 拷进 workspace(SKILL.md 里 references/xxx 可读);
- N 次 spawn,每次 dump raw stream-json 成 run-{N}/transcript.jsonl,打通 audit 闭环。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from skill_eval.execution.cli_runner import ClaudeRunner
from skill_eval.execution.runner import _copy_skill_references
from skill_eval.models import RunOutput

ProgressCallback = Callable[..., None]

# 拷贝 --cwd 时忽略的大目录(.git/.venv 等),避免拷贝爆炸
_IGNORE_ON_COPY = shutil.ignore_patterns(
    ".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"
)


def run_live_task(
    skill_path: Path,
    task: str,
    cwd: Path | None = None,
    runs: int = 1,
    model: str | None = None,
    timeout: int = 300,
    max_turns: int = 10,
    output_dir: Path = Path("output"),
    on_progress: ProgressCallback | None = None,
) -> Path:
    """跑路径1 真任务模式,返回 output_dir。

    产物:output_dir/workspace/(隔离拷贝)、output_dir/run-{N}/transcript.jsonl
    (raw stream-json,可直接喂 `audit --transcript`)、run-{N}/run_debug.json。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. --cwd 拷贝隔离;无 cwd 则 spawn cwd 用 skill 目录(纯对话任务无文件材料)
    if cwd is not None:
        workspace = output_dir / "workspace"
        shutil.copytree(cwd, workspace, ignore=_IGNORE_ON_COPY, dirs_exist_ok=True)
    else:
        workspace = skill_path

    # 2. skill references/ 拷进 workspace(SKILL.md 里 references/xxx 可读)
    _copy_skill_references(skill_path, workspace)

    # 3. N 次 spawn
    runner = ClaudeRunner()
    for run_num in range(1, runs + 1):
        run_dir = output_dir / f"run-{run_num}"
        run_dir.mkdir(parents=True, exist_ok=True)

        run_output: RunOutput = runner.run(
            prompt=task,
            skill_path=skill_path,
            model=model,
            timeout=timeout,
            cwd=workspace,
            max_turns=max_turns,
        )

        # 4. dump raw stream-json 成 transcript.jsonl(打通 audit 闭环)
        if run_output.stdout:
            (run_dir / "transcript.jsonl").write_text(run_output.stdout, encoding="utf-8")

        run_debug = {
            "run_num": run_num,
            "task": task[:500],
            "duration_ms": run_output.duration_ms,
            "total_tokens": run_output.total_tokens,
            "num_turns": run_output.num_turns,
            "stop_reason": run_output.stop_reason,
            "tool_calls": run_output.tool_calls,
            "result_text": run_output.result_text[:2000] if run_output.result_text else "",
        }
        (run_dir / "run_debug.json").write_text(
            json.dumps(run_debug, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if on_progress:
            on_progress(run_num, runs, run_output)

    return output_dir
