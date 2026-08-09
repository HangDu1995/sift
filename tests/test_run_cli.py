"""run CLI:--task 分支的 --executor flag + 校验 + 错误兜底。

测 --task 路径1 真任务模式的执行器选择:
- --executor cc|opencode|cc,opencode 透传给 run_live_task 的 executors 参数
- 未知名 exit 2(白名单校验)
- ExecutionError → 友好错误 exit 3,非裸 traceback

monkeypatch 掉 run_live_task + _check_claude_or_exit,不 spawn 真 cc / opencode。
run_live_task 在 run.py 模块顶层 import(便于 patch
`skill_eval.cli.commands.run.run_live_task`)。
"""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from skill_eval.cli.main import app

runner = CliRunner()


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text(
        "---\nname: sv\ndescription: d\n---\n# b\n", encoding="utf-8"
    )
    return sp


def test_run_task_executor_opencode_passed(monkeypatch, tmp_path):
    """--executor opencode → run_live_task 收到 executors=['opencode']。"""
    captured = {}

    def fake_live(**kw):
        captured.update(kw)
        (kw["output_dir"] / "run-1" / "transcript.json").parent.mkdir(
            parents=True, exist_ok=True
        )
        (kw["output_dir"] / "run-1" / "transcript.json").write_text(
            "{}", encoding="utf-8"
        )
        return kw["output_dir"]

    monkeypatch.setattr("skill_eval.cli.commands.run.run_live_task", fake_live)
    monkeypatch.setattr(
        "skill_eval.cli.commands.run._check_claude_or_exit", lambda: None
    )
    sp = _skill(tmp_path)
    out = tmp_path / "o"
    r = runner.invoke(
        app,
        [
            "run", str(sp), "--task", "t", "--cwd", str(tmp_path),
            "--executor", "opencode", "-o", str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["executors"] == ["opencode"]


def test_run_task_executor_csv_split(monkeypatch, tmp_path):
    """--executor cc,opencode → executors=['cc', 'opencode'](逗号分隔多执行器)。"""
    captured = {}

    def fake_live(**kw):
        captured.update(kw)
        return kw["output_dir"]

    monkeypatch.setattr("skill_eval.cli.commands.run.run_live_task", fake_live)
    monkeypatch.setattr(
        "skill_eval.cli.commands.run._check_claude_or_exit", lambda: None
    )
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        [
            "run", str(sp), "--task", "t", "--executor", "cc,opencode",
            "--cwd", str(tmp_path), "-o", str(tmp_path / "o"),
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["executors"] == ["cc", "opencode"]


def test_run_task_unknown_executor_exit2(monkeypatch, tmp_path):
    """未知 executor → exit 2,错误信息含未知名 + 合法名提示。"""
    monkeypatch.setattr(
        "skill_eval.cli.commands.run._check_claude_or_exit", lambda: None
    )
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        [
            "run", str(sp), "--task", "t", "--executor", "bogus",
            "--cwd", str(tmp_path), "-o", str(tmp_path / "o"),
        ],
    )
    assert r.exit_code == 2
    assert "bogus" in r.output and "cc" in r.output


def test_run_task_executor_error_clean_not_traceback(monkeypatch, tmp_path):
    """ExecutionError(opencode 缺失等)→ 友好错误 exit 3,非裸 traceback。"""
    from skill_eval.errors import ExecutionError

    def boom(**kw):
        raise ExecutionError("opencode CLI 未找到")

    monkeypatch.setattr("skill_eval.cli.commands.run.run_live_task", boom)
    monkeypatch.setattr(
        "skill_eval.cli.commands.run._check_claude_or_exit", lambda: None
    )
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        [
            "run", str(sp), "--task", "t", "--executor", "opencode",
            "--cwd", str(tmp_path), "-o", str(tmp_path / "o"),
        ],
    )
    assert r.exit_code == 3
    assert "opencode CLI 未找到" in r.output
    # try/except 兜住→干净 typer.Exit(3)=SystemExit(3);无 try/except 则裸 ExecutionError 透出,r.exception 是它而非 SystemExit
    assert isinstance(r.exception, SystemExit)


def test_run_task_opencode_not_blocked_by_cc_check(monkeypatch, tmp_path):
    """--executor opencode 不该被 cc-check 挡(用户可能只装了 opencode 没装 claude)。"""
    import typer as _typer
    def fake_check():  # 模拟 claude 缺失
        raise _typer.Exit(code=3)
    monkeypatch.setattr("skill_eval.cli.commands.run._check_claude_or_exit", fake_check)
    monkeypatch.setattr("skill_eval.cli.commands.run.run_live_task", lambda **kw: kw["output_dir"])
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp), "--task", "t", "--executor", "opencode",
                            "--cwd", str(tmp_path), "-o", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output  # cc-check 被跳过 → 不 exit 3


def test_run_task_cc_still_checked(monkeypatch, tmp_path):
    """默认(或 --executor cc)仍走 cc-check。"""
    called = []
    monkeypatch.setattr("skill_eval.cli.commands.run._check_claude_or_exit", lambda: called.append(1))
    monkeypatch.setattr("skill_eval.cli.commands.run.run_live_task", lambda **kw: kw["output_dir"])
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp), "--task", "t", "--cwd", str(tmp_path), "-o", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    assert called  # 默认 cc → 走了 cc-check


def test_run_cli_invalid_kind_exits_2(tmp_path):
    """--kind 只接受 skill / agent;非法值 exit 2。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp), "--kind", "workflow", "-o", str(tmp_path / "o")])
    assert r.exit_code == 2
    assert "skill / agent" in r.output


def test_run_cli_agent_rejects_skill_b(tmp_path):
    """--kind agent(单臂)与 --skill-b(A/B)互斥 → exit 2。"""
    sp = _skill(tmp_path)
    sb = tmp_path / "b"
    sb.mkdir()
    (sb / "SKILL.md").write_text("---\nname: b\ndescription: d\n---\nx\n", encoding="utf-8")
    r = runner.invoke(app, ["run", str(sp), "--kind", "agent", "--skill-b", str(sb), "-o", str(tmp_path / "o")])
    assert r.exit_code == 2
    assert "互斥" in r.output


def test_run_cli_agent_rejects_task(tmp_path):
    """--kind agent(evals 路径)与 --task(路径1 真任务)互斥 → exit 2。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp), "--kind", "agent", "--task", "do thing",
                            "--cwd", str(tmp_path), "-o", str(tmp_path / "o")])
    assert r.exit_code == 2
    assert "互斥" in r.output


def test_run_cli_agent_kind_threads(monkeypatch, tmp_path):
    """--kind agent → _run_ab_eval 收 kind='agent'(单臂;evals 路径)。"""
    captured = {}

    def fake_run_ab_eval(**kw):
        captured.update(kw)
        return (None, tmp_path / "out")

    monkeypatch.setattr("skill_eval.cli.commands.run._run_ab_eval", fake_run_ab_eval)
    monkeypatch.setattr("skill_eval.cli.commands.run._check_claude_or_exit", lambda: None)
    monkeypatch.setattr("skill_eval.cli.commands.run._emit_static_and_vet", lambda *a, **k: None)
    monkeypatch.setattr("skill_eval.cli.commands.run._emit_report", lambda *a, **k: None)
    sp = tmp_path / "agent.md"
    sp.write_text("---\nname: r\ndescription: d\n---\nbody\n", encoding="utf-8")
    evals = tmp_path / "evals.json"
    evals.write_text('{"skill_name":"r","evals":[{"id":1,"prompt":"do"}]}', encoding="utf-8")
    r = runner.invoke(app, ["run", str(sp), "--kind", "agent", "--evals", str(evals),
                            "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("kind") == "agent"
