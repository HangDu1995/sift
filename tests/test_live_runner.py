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


def test_format_run_summary_renders_metrics_and_result_head():
    """_format_run_summary 把 run_debug dict 格式化成 console 摘要行。

    解 §50 "run_debug operational metrics 孤儿":run --task 跑完逐 run 打印一行,
    让 duration/tokens/turns/stop/tool_calls + 结果头部不再静默。纯函数(不 spawn cc)。
    tool_calls 是 dict[str,int](工具名→次数),摘要用 sum(values)=总调用次数。
    """
    from skill_eval.cli.commands.run import _format_run_summary

    d = {
        "run_num": 2,
        "duration_ms": 12345,
        "num_turns": 5,
        "total_tokens": 4321,
        "stop_reason": "end_turn",
        "tool_calls": {"Read": 3, "Write": 1},
        "result_text": "任务完成\n产物已写: report.json",
    }
    s = _format_run_summary(d)
    assert "run-2" in s
    assert "12.3s" in s  # 12345ms → 12.3s
    assert "5 turns" in s
    assert "4321 tokens" in s
    assert "end_turn" in s
    assert "4 tool_calls" in s  # sum(dict values) = 3 + 1
    assert "report.json" in s  # 结果头部(result_text 单行化)


def test_format_run_summary_handles_missing_fields():
    """缺字段不崩,给合理默认(0 / —)。"""
    from skill_eval.cli.commands.run import _format_run_summary

    s = _format_run_summary({})
    assert "run-?" in s  # run_num 缺省
    assert "0 tokens" in s
    assert "0 tool_calls" in s
    assert "—" in s  # stop_reason 缺省


def _skill_with_md(tmp_path):
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: sv\ndescription: d\n---\n# b\n", encoding="utf-8")
    return sp


def _cwd_dir(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    (d / "f.txt").write_text("x", encoding="utf-8")
    return d


def test_run_live_task_multi_executor_namespaced_dirs(tmp_path, monkeypatch):
    """executors=[cc,opencode] → run-1-cc/transcript.jsonl + run-1-opencode/transcript.json。"""
    from skill_eval.execution import live_runner as lr
    from skill_eval.models import RunOutput

    def fake_factory(name):
        class F:
            def run(self, **kw):
                return RunOutput(exit_code=0, stdout=f"{name}-TRANSCRIPT")
        return F()

    monkeypatch.setattr(lr, "make_live_runner", fake_factory)
    out = tmp_path / "out"
    lr.run_live_task(
        skill_path=_skill_with_md(tmp_path), task="t", cwd=_cwd_dir(tmp_path),
        runs=1, executors=["cc", "opencode"], output_dir=out,
    )
    assert (out / "run-1-cc" / "transcript.jsonl").read_text(encoding="utf-8") == "cc-TRANSCRIPT"
    assert (out / "run-1-opencode" / "transcript.json").read_text(encoding="utf-8") == "opencode-TRANSCRIPT"


def test_run_live_task_single_executor_no_suffix_backward_compat(tmp_path, monkeypatch):
    """单执行器(任一)→ run-1/(无 -exec 后缀),向后兼容老 audit 提示。"""
    from skill_eval.execution import live_runner as lr
    from skill_eval.models import RunOutput

    monkeypatch.setattr(lr, "make_live_runner",
                        lambda name: type("F", (), {"run": lambda self, **kw: RunOutput(exit_code=0, stdout="X")})())
    out = tmp_path / "out"
    lr.run_live_task(
        skill_path=_skill_with_md(tmp_path), task="t", cwd=_cwd_dir(tmp_path),
        runs=1, executors=["opencode"], output_dir=out,
    )
    assert (out / "run-1" / "transcript.json").exists()
    assert not (out / "run-1-opencode").exists()


def test_make_live_runner_unknown_raises(tmp_path):
    from skill_eval.execution import live_runner as lr
    from skill_eval.errors import InputError
    import pytest
    with pytest.raises(InputError, match="未知 executor"):
        lr.make_live_runner("foo")
