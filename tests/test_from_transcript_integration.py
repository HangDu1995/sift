# tests/test_from_transcript_integration.py
import json
from pathlib import Path
from skill_eval.evals.generator import EvalInitGenerator
from skill_eval.evals.transcript import build_digest
from skill_eval.scoring.programmatic import ProgrammaticChecker


def _rec(role, content):
    return {"message": {"role": role, "content": content}}

def _tu(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}

def _tr(tid, text):
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}

def test_closed_loop_pass_on_good_fail_on_sabotaged(tmp_path, monkeypatch):
    # 1) 合成 transcript: Read calc.py + Write sub/cli.py + Bash 产 out.txt
    tp = tmp_path / "sess.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in [
        _rec("user", "造 cli"),
        _rec("assistant", [_tu("r1", "Read", {"file_path": "/proj/calc.py"})]),
        _tr("r1", "1\tdef evaluate(e): ...\n"),
        _rec("assistant", [_tu("w1", "Write", {"file_path": "/proj/sub/cli.py",
              "content": "from calc import evaluate\nprint(evaluate('1+1'))\n"})]),
        _rec("assistant", [_tu("b1", "Bash", {"command": "echo x > /proj/sub/out.txt", "description": ""})]),
        _tr("b1", ""),
    ]), encoding="utf-8")

    digest = build_digest(tp)
    assert digest.manifest == {"calc.py", "sub/cli.py"}
    assert digest.unreconstructed == ["sub/out.txt"]

    # 2) mock LLM 返回裸 filename + 无 fixture 的 CUSTOM_SCRIPT(应被后处理补全/丢弃)
    fake = json.dumps({"skill_name": "calc", "evals": [{
        "id": 1, "name": "L2-cli", "level": "L2", "prompt": "造 cli",
        "expected_output": "", "expectations": [],
        "assertions": [
            {"name": "cli", "type": "FILE_EXISTS", "params": {"filename": "cli.py"}},
            {"name": "imp", "type": "CONTAINS_PATTERN", "params": {"pattern": "import evaluate", "path": "cli.py"}},
            {"name": "run", "type": "CUSTOM_SCRIPT", "params": {"script": "check.py", "args": []}},
        ],
        "fixtures": [],
    }]})
    monkeypatch.setattr("skill_eval.evals.generator.call_claude", lambda *a, **k: fake)

    sp = tmp_path / "skill"; sp.mkdir()
    (sp / "SKILL.md").write_text("---\nname: calc\ndescription: d\n---\n# b\n", encoding="utf-8")
    es = EvalInitGenerator().generate_from_transcript(sp, tp)

    # 3) 后处理断言:filename->sub/cli.py,CUSTOM_SCRIPT 被 drop(无 fixture)
    a = es.evals[0].assertions
    types = [x.type.value for x in a]
    assert "CUSTOM_SCRIPT" not in types                      # 未配对->丢弃
    assert a[0].params["filename"] == "sub/cli.py"           # 裸名补全
    assert a[1].params["path"] == "sub/cli.py"

    # 4) 重建 GOOD output_dir 跑真 scorer -> 全 PASS
    good = tmp_path / "good"; (good / "sub").mkdir(parents=True)
    (good / "sub" / "cli.py").write_text("from calc import evaluate\n", encoding="utf-8")
    checker = ProgrammaticChecker()
    res = checker.check(es.evals[0], good, transcript="", tool_trace=[])
    assert all(r.passed for r in res), [r.evidence for r in res]

    # 5) SABOTAGED(空目录)-> FAIL
    bad = tmp_path / "bad"; bad.mkdir()
    res2 = checker.check(es.evals[0], bad, transcript="", tool_trace=[])
    assert any(not r.passed for r in res2)
