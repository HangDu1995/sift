"""路径1 真任务模式(run_live_task)测试。

验证:--cwd 拷贝隔离(cc 不碰原目录)、skill references 拷进考场、
每次 run dump raw stream-json 成 transcript.jsonl、--runs N 跑 N 次产 N 份 trace。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from skill_eval.execution.live_runner import run_live_task
from skill_eval.models import RunOutput


def _fake_stream_json() -> str:
    """模拟 claude -p --output-format stream-json 的最小输出(assistant + result event)。"""
    return (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"reading code"}]}}\n'
        '{"type":"result","result":"done","usage":{"input_tokens":10,"output_tokens":5}}\n'
    )


def _make_run_output() -> RunOutput:
    return RunOutput(
        exit_code=0,
        stdout=_fake_stream_json(),
        transcript="reading code",
        total_tokens=15,
    )


def _make_skill(skill_dir: Path) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: t\ndescription: test\n---\n# body\n", encoding="utf-8")


def test_live_task_copies_cwd_to_workspace(tmp_path: Path) -> None:
    """--cwd 拷贝到 output_dir/workspace;spawn 用 workspace 作 cwd(隔离),不是原 cwd。"""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "target.py").write_text("print('hi')", encoding="utf-8")
    skill = tmp_path / "skill"
    _make_skill(skill)
    out = tmp_path / "out"

    with patch("skill_eval.execution.live_runner.ClaudeRunner") as cr:
        cr.return_value.run.return_value = _make_run_output()
        run_live_task(skill_path=skill, task="review target.py", cwd=cwd, runs=1, output_dir=out)

    workspace = out / "workspace"
    assert (workspace / "target.py").read_text(encoding="utf-8") == "print('hi')"
    # spawn 隔离:cwd 是 workspace 拷贝,不是原 repo
    assert cr.return_value.run.call_args.kwargs["cwd"] == workspace


def test_live_task_copies_skill_references(tmp_path: Path) -> None:
    """skill references/ 拷进 workspace,使 SKILL.md 里 references/xxx 可读。"""
    skill = tmp_path / "skill"
    _make_skill(skill)
    (skill / "references").mkdir()
    (skill / "references" / "patterns.md").write_text("patterns", encoding="utf-8")
    cwd = tmp_path / "repo"
    cwd.mkdir()
    out = tmp_path / "out"

    with patch("skill_eval.execution.live_runner.ClaudeRunner") as cr:
        cr.return_value.run.return_value = _make_run_output()
        run_live_task(skill_path=skill, task="t", cwd=cwd, runs=1, output_dir=out)

    assert (out / "workspace" / "references" / "patterns.md").read_text(encoding="utf-8") == "patterns"


def test_live_task_dumps_transcript_jsonl(tmp_path: Path) -> None:
    """每次 run dump raw stream-json 成 run-{N}/transcript.jsonl(打通 audit 闭环)。"""
    skill = tmp_path / "skill"
    _make_skill(skill)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    out = tmp_path / "out"

    with patch("skill_eval.execution.live_runner.ClaudeRunner") as cr:
        cr.return_value.run.return_value = _make_run_output()
        run_live_task(skill_path=skill, task="t", cwd=cwd, runs=2, output_dir=out)

    for n in (1, 2):
        dumped = (out / f"run-{n}" / "transcript.jsonl").read_text(encoding="utf-8")
        assert dumped == _fake_stream_json()


def test_live_task_runs_n_times(tmp_path: Path) -> None:
    """--runs N → spawn N 次,产 N 份 transcript。"""
    skill = tmp_path / "skill"
    _make_skill(skill)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    out = tmp_path / "out"

    with patch("skill_eval.execution.live_runner.ClaudeRunner") as cr:
        cr.return_value.run.return_value = _make_run_output()
        run_live_task(skill_path=skill, task="t", cwd=cwd, runs=3, output_dir=out)

    assert cr.return_value.run.call_count == 3


def test_live_task_no_cwd_uses_skill_dir(tmp_path: Path) -> None:
    """没给 --cwd:不拷贝,spawn cwd 用 skill 目录(无文件材料的纯任务仍可跑)。"""
    skill = tmp_path / "skill"
    _make_skill(skill)
    out = tmp_path / "out"

    with patch("skill_eval.execution.live_runner.ClaudeRunner") as cr:
        cr.return_value.run.return_value = _make_run_output()
        run_live_task(skill_path=skill, task="t", cwd=None, runs=1, output_dir=out)

    assert cr.return_value.run.call_args.kwargs["cwd"] == skill
