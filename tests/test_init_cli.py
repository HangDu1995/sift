# tests/test_init_cli.py
from typer.testing import CliRunner
from skill_eval.cli.main import app
from skill_eval.models import EvalSet, EvalCase

runner = CliRunner()

def _fake_evalset():
    return EvalSet(skill_name="s", evals=[EvalCase(id=1, name="n", prompt="p", level="L1")])

def _skill_dir(tmp_path):
    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: s\ndescription: d\n---\n# b\n", encoding="utf-8")
    return sp

def test_init_without_flag_calls_generate_with_llm(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: called.__setitem__("default", True) or _fake_evalset())
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: called.__setitem__("transcript", True) or _fake_evalset())
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("default") is True and "transcript" not in called

def test_init_with_from_transcript_routes(monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: called.__setitem__("tp", tp) or _fake_evalset())
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: called.__setitem__("default", True) or _fake_evalset())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert called.get("tp") == tp and "default" not in called


def _fake_evalset_degraded():
    return EvalSet(skill_name="s", evals=[
        EvalCase(id=1, name="transcript-turn-1", prompt="p", level="L1", flags=["degraded:no-llm"])
    ])


def test_init_warns_on_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset_degraded())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("no-llm" in p for p in printed), printed


def test_init_no_warning_without_fallback_flag(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("no-llm" in p for p in printed), printed


def test_init_threads_model_flag_to_generator(monkeypatch, tmp_path):
    """init --model 接线:flag 须透到 EvalInitGenerator(model=...)。曾硬编码 model=None,
    flag 是静默 no-op 死标志(对照 add 已正确接线)。"""
    captured = {}

    def fake_init(self, model=None, timeout=300, **_kw):
        captured["model"] = model

    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.__init__", fake_init)
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: _fake_evalset())
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "--model", "sonnet", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("model") == "sonnet", f"--model 没透到 generator: {captured}"


def test_init_threads_timeout_flag_to_generator(monkeypatch, tmp_path):
    """init --timeout 接线:flag 须透到 EvalInitGenerator(timeout=...);默认 600。"""
    captured = {}

    def fake_init(self, model=None, timeout=600, **_kw):
        captured["timeout"] = timeout

    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.__init__", fake_init)
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: _fake_evalset())
    sp = _skill_dir(tmp_path)
    # 显式传 --timeout
    r = runner.invoke(app, ["init", str(sp), "--timeout", "300", "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("timeout") == 300, f"--timeout 没透到 generator: {captured}"
    # 默认(不传)→ 600
    captured.clear()
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out2")])
    assert r.exit_code == 0, r.output
    assert captured.get("timeout") == 600, f"默认 timeout 应为 600: {captured}"


def test_init_kind_threads_flag(monkeypatch, tmp_path):
    """init --kind skill 接线:flag 须透到 generate_from_transcript(kind="skill")。"""
    captured = {}

    def fake(self, sp, tp, on_heartbeat=None, kind=None):
        captured["kind"] = kind
        return _fake_evalset()

    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript", fake)
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: _fake_evalset())
    sp = _skill_dir(tmp_path)
    tp = tmp_path / "t.jsonl"; tp.write_text("{}", encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp), "--kind", "skill",
                            "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert captured.get("kind") == "skill", f"--kind 没透到 generator: {captured}"


def test_init_warns_on_multiskill_without_kind(monkeypatch, tmp_path):
    """多 skill transcript + 不传 --kind → 警告(防静默串味:把别 skill 活动当本 skill 挖)。"""
    import json
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset())
    sp = _skill_dir(tmp_path)  # name "s"
    tp = tmp_path / "multi.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "s"}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "s2", "name": "Skill", "input": {"skill": "other"}}]}},
    ]), encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp),
                            "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("涉及" in p and "skill" in p for p in printed), printed


def test_init_no_warning_on_single_skill(monkeypatch, tmp_path):
    """单 skill transcript + 不传 --kind → 不警告。"""
    import json
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_from_transcript",
                        lambda self, sp, tp, on_heartbeat=None, kind=None: _fake_evalset())
    sp = _skill_dir(tmp_path)  # name "s"
    tp = tmp_path / "single.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "s"}}]}},
    ]), encoding="utf-8")
    r = runner.invoke(app, ["init", str(sp), "--from-transcript", str(tp),
                            "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("涉及" in p and "skill" in p for p in printed), printed


def test_init_notes_same_prompt_group(monkeypatch, tmp_path):
    """生成出同 prompt 多 scenario 题(带 split flag)→ init 打 ℹ 提示「非重复」。
    防读者把同 prompt 多道误读成 bug(复现 log-error-analyzer-zh 那次误读场景)。
    断言用「同 prompt」+「非重复」整串——distinctive,避开 tmp_path 假绿陷阱。"""
    evalset = EvalSet(skill_name="s", evals=[
        EvalCase(id=i, name=f"scenario-{i}", prompt="分析 logs/app.log 里的错误",
                 flags=["split:3-evals-conflict"])
        for i in (1, 2, 3)
    ])
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("同 prompt" in p and "非重复" in p for p in printed), printed


def test_init_no_note_when_prompts_unique(monkeypatch, tmp_path):
    """每道 prompt 都不同 → 不打同 prompt 提示(免噪音)。"""
    evalset = EvalSet(skill_name="s", evals=[
        EvalCase(id=1, name="a", prompt="题一"), EvalCase(id=2, name="b", prompt="题二")
    ])
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert not any("同 prompt" in p for p in printed), printed


def test_init_lists_programmatic_count_per_eval(monkeypatch, tmp_path):
    """init 逐条输出须显示客观题(assertions)数量,不只主观 expectations。
    曾只印 expectations+fixtures、漏 assertions(客观题是确定性最强的部分)。
    断言用「N assertions, M expectations」整串——避免测试名进 tmp_path 造成假绿
    (假绿陷阱:断言词与 tmp_path 来源重合会恒真,见记忆 test_init_warns_on_degraded)。"""
    from skill_eval.models import Assertion, AssertionType
    printed = []
    monkeypatch.setattr("skill_eval.cli.commands.init.console.print",
                        lambda *a, **k: printed.append(str(a)))
    evalset = EvalSet(skill_name="s", evals=[EvalCase(
        id=1, name="n", prompt="p", level="L1",
        expectations=["e1", "e2", "e3"],
        assertions=[Assertion(name="a", type=AssertionType.FILE_EXISTS),
                    Assertion(name="b", type=AssertionType.CONTAINS_PATTERN)],
    )])
    monkeypatch.setattr("skill_eval.evals.generator.EvalInitGenerator.generate_with_llm",
                        lambda self, sp: evalset)
    sp = _skill_dir(tmp_path)
    r = runner.invoke(app, ["init", str(sp), "-o", str(tmp_path / "out")])
    assert r.exit_code == 0, r.output
    assert any("2 assertions, 3 expectations" in p for p in printed), printed
