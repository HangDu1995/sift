"""run CLI:--task 分支的 --executor flag + 校验 + 错误兜底。

测 --task 路径1 真任务模式的执行器选择:
- --executor cc|opencode|cc,opencode 透传给 run_live_task 的 executors 参数
- 未知名 exit 2(白名单校验)
- ExecutionError → 友好错误 exit 3,非裸 traceback

monkeypatch 掉 run_live_task + _check_claude_or_exit,不 spawn 真 cc / opencode。
run_live_task 在 run.py 模块顶层 import(便于 patch
`sift.cli.commands.run.run_live_task`)。
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from sift.cli.main import app

runner = CliRunner()


def _skill(tmp_path: Path) -> Path:
    sp = tmp_path / "skill"
    sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: sv\ndescription: d\n---\n# b\n", encoding="utf-8")
    return sp


def test_run_task_executor_opencode_passed(monkeypatch, tmp_path):
    """--executor opencode → run_live_task 收到 executors=['opencode']。"""
    captured = {}

    def fake_live(**kw):
        captured.update(kw)
        (kw["options"].output_dir / "run-1" / "transcript.json").parent.mkdir(parents=True, exist_ok=True)
        (kw["options"].output_dir / "run-1" / "transcript.json").write_text("{}", encoding="utf-8")
        return kw["options"].output_dir

    monkeypatch.setattr("sift.cli.commands.run.run_live_task", fake_live)
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    sp = _skill(tmp_path)
    out = tmp_path / "o"
    r = runner.invoke(
        app,
        [
            "run",
            str(sp),
            "--task",
            "t",
            "--cwd",
            str(tmp_path),
            "--executor",
            "opencode",
            "-o",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    assert captured["options"].executors == ["opencode"]


def test_run_task_executor_csv_split(monkeypatch, tmp_path):
    """--executor cc,opencode → executors=['cc', 'opencode'](逗号分隔多执行器)。"""
    captured = {}

    def fake_live(**kw):
        captured.update(kw)
        return kw["options"].output_dir

    monkeypatch.setattr("sift.cli.commands.run.run_live_task", fake_live)
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        _live_task_args(str(sp), "cc,opencode", str(tmp_path), str(tmp_path / "o")),
    )
    assert r.exit_code == 0, r.output
    assert captured["options"].executors == ["cc", "opencode"]


def test_run_task_unknown_executor_exit2(monkeypatch, tmp_path):
    """未知 executor → exit 2,错误信息含未知名 + 合法名提示。"""
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        _live_task_args(str(sp), "bogus", str(tmp_path), str(tmp_path / "o")),
    )
    assert r.exit_code == 2
    assert "bogus" in r.output and "cc" in r.output


def test_run_task_executor_error_clean_not_traceback(monkeypatch, tmp_path):
    """ExecutionError(opencode 缺失等)→ 友好错误 exit 3,非裸 traceback。"""
    from sift.errors import ExecutionError

    def boom(**kw):
        raise ExecutionError("opencode CLI 未找到")

    monkeypatch.setattr("sift.cli.commands.run.run_live_task", boom)
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        _live_task_args(str(sp), "opencode", str(tmp_path), str(tmp_path / "o")),
        catch_exceptions=False,
    )
    assert r.exit_code == 3
    assert "opencode CLI 未找到" in r.output
    # catch_exceptions=False 让未处理的 ExecutionError 直接令测试失败；预期 CLI 退出仍由 Click 捕获。
    assert "Traceback" not in r.output


def test_run_task_opencode_not_blocked_by_cc_check(monkeypatch, tmp_path):
    """--executor opencode 不该被 cc-check 挡(用户可能只装了 opencode 没装 claude)。"""
    import typer as _typer

    def fake_check():  # 模拟 claude 缺失
        raise _typer.Exit(code=3)

    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), fake_check)
    monkeypatch.setattr("sift.cli.commands.run.run_live_task", lambda **kw: kw["options"].output_dir)
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        _live_task_args(str(sp), "opencode", str(tmp_path), str(tmp_path / "o")),
    )
    assert r.exit_code == 0, r.output  # cc-check 被跳过 → 不 exit 3


def test_run_task_cc_still_checked(monkeypatch, tmp_path):
    """默认(或 --executor cc)仍走 cc-check。"""
    called = []
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: called.append(1))
    monkeypatch.setattr("sift.cli.commands.run.run_live_task", lambda **kw: kw["options"].output_dir)
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp), "--task", "t", "--cwd", str(tmp_path), "-o", str(tmp_path / "o")])
    assert r.exit_code == 0, r.output
    assert called  # 默认 cc → 走了 cc-check


def test_run_cli_invalid_kind_exits_2(tmp_path):
    """--kind 只接受 skill / agent;非法值 exit 2。"""
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["eval", str(sp), "--kind", "workflow", "-o", str(tmp_path / "o")])
    assert r.exit_code == 2
    assert "skill / agent" in r.output


def test_run_cli_agent_rejects_skill_b(tmp_path):
    """--kind agent 与 --skill-b(A/B)互斥 → exit 2。"""
    sp = _skill(tmp_path)
    sb = tmp_path / "b"
    sb.mkdir()
    (sb / "SKILL.md").write_text("---\nname: b\ndescription: d\n---\nx\n", encoding="utf-8")
    r = runner.invoke(app, ["eval", str(sp), "--kind", "agent", "--skill-b", str(sb), "-o", str(tmp_path / "o")])
    assert r.exit_code == 2
    assert "互斥" in r.output


def test_run_cli_rejects_kind_option(tmp_path):
    """拆分后 run(真任务)无 --kind:typer 未知选项 → exit 2(原 agent+task 互斥测试退役)。"""
    sp = _skill(tmp_path)
    r = runner.invoke(
        app,
        ["run", str(sp), "--task", "do thing", "--kind", "agent", "--cwd", str(tmp_path), "-o", str(tmp_path / "o")],
    )
    assert r.exit_code == 2
    assert "No such option" in r.output


def test_run_cli_task_required(tmp_path):
    """--task 必填:不给 → exit 2 Missing option(钉死 Option(..., "--task") 写法,
    防 'Option("--task", ...)' 陷阱形静默变可选、拿字面量当任务文本跑)。
    """
    sp = _skill(tmp_path)
    r = runner.invoke(app, ["run", str(sp)])
    assert r.exit_code == 2
    assert "Missing option" in r.output


def test_run_cli_agent_kind_threads(monkeypatch, tmp_path):
    """--kind agent → _run_ab_eval 收 kind='agent'(agent 评测;evals 路径)。"""
    captured = {}

    def fake_run_ab_eval(**kw):
        captured.update(kw)
        return (None, tmp_path / "out")

    monkeypatch.setattr("sift.cli.commands.run._run_ab_eval", fake_run_ab_eval)
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    monkeypatch.setattr("sift.cli.commands.run._emit_static_and_vet", lambda *a, **k: None)
    monkeypatch.setattr("sift.cli.commands.run._emit_report", lambda *a, **k: None)
    sp = tmp_path / "agent.md"
    sp.write_text("---\nname: r\ndescription: d\n---\nbody\n", encoding="utf-8")
    evals = tmp_path / "evals.json"
    evals.write_text(('{"skill_name":"r","evals":[{"id":1,"prompt":"do"}]}'), encoding="utf-8")
    r = runner.invoke(app, ["eval", str(sp), "--kind", "agent", "--evals", str(evals), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured["run_config"].kind == "agent"


def test_run_cli_agent_skips_scorecard_and_vet(monkeypatch, tmp_path):
    """kind=agent → 不产 scorecard.json/vet-report.json。

    scorecard(11 项 SKILL.md 结构检查)+ vet(structure-check 报 'Missing SKILL.md')
    的检查对象是 SKILL.md 目录结构,对 agent.md(单文件)跑出假信号——实测 vet 误报
    BLOCKED、scorecard 9/11 因非 SKILL.md 结构 FAIL。故 agent 模式 run 末尾跳过这俩。
    不 mock _emit_static_and_vet,让它真跑(对 agent.md)验证 kind=agent 早返不产。
    """
    out = tmp_path / "out"
    out.mkdir()  # iteration_dir 预建(_emit_static_and_vet 写入目标)
    monkeypatch.setattr("sift.cli.commands.run._run_ab_eval", lambda **kw: (None, out))
    monkeypatch.setattr(("sift.cli.commands.run._check_claude_or_exit"), lambda: None)
    monkeypatch.setattr("sift.cli.commands.run._emit_report", lambda *a, **k: None)
    sp = tmp_path / "agent.md"
    sp.write_text("---\nname: r\ndescription: d\n---\nbody\n", encoding="utf-8")
    evals = tmp_path / "evals.json"
    evals.write_text(('{"skill_name":"r","evals":[{"id":1,"prompt":"do"}]}'), encoding="utf-8")
    r = runner.invoke(app, ["eval", str(sp), "--kind", "agent", "--evals", str(evals), "-o", str(out)])
    assert r.exit_code == 0, r.output
    assert not (out / "scorecard.json").exists()  # agent 不产(SKILL.md 结构检查不适用)
    assert not (out / "vet-report.json").exists()


def test_resolve_skip_compliance_defaults_on():
    """compliance 默认开(不再 runs<3 跳过):单 transcript 过程对账本就有效。
    --no-compliance(True)→ 跳过;不传(None)/ --compliance(False)→ 跑。
    """
    from sift.cli.commands.run import _resolve_skip_compliance

    assert _resolve_skip_compliance(None) is False  # 默认开
    assert _resolve_skip_compliance(True) is True  # --no-compliance 跳过
    assert _resolve_skip_compliance(False) is False  # --compliance 显式开


def _live_task_args(skill, executor, cwd, output):
    return ["run", skill, "--task", "t", "--executor", executor, "--cwd", cwd, "-o", output]
