# tests/test_audit_root.py
"""audit --kind root CLI 接线测试(cc + opencode 双平台)。

照 test_audit_by_skill.py 模式:mock TranscriptAuditor(不真跑 LLM),验证:
- cc --kind root:主作用域(parent_tool_use_id 为空)切给 auditor,排除子 agent 内部
- opencode --kind root:以 session.info.agent 为 target(复用 slice_opencode_by_agent)
- target 名忽略(root 不按声明名切,声明名 "wf" ≠ 主 agent)
- opencode session 缺 info.agent → exit 2
"""

from __future__ import annotations

import json
from pathlib import Path

from cli_helpers import invoke_cli
from report_fixtures import root_audit_report

from sift.audit.judge_context import AuditOptions
from sift.models.audit_models import (
    AuditReport,
)

from .helpers import create_root_skill as _skill_dir
from .helpers import write_jsonl as _write_jsonl


def _asst(t):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}


def _tool_use(tid, name, inp):
    return {"message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _agent(tid):
    return {
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "x"}}],
        }
    }


def _tool_result(tid, text):
    return {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def _in_sub(parent, rec):
    rec["parent_tool_use_id"] = parent
    return rec


def _fake_report() -> AuditReport:
    return root_audit_report("orchestrate")


class _FakeAuditor:
    def __init__(self):
        self.calls: list[dict] = []

    def audit(self, skill_name, skill_body, transcripts, options: AuditOptions = AuditOptions()):
        aux_tool_traces = options.aux_tool_traces
        self.calls.append(
            {"skill_name": skill_name, "transcripts": list(transcripts), "aux_tool_traces": aux_tool_traces}
        )
        return _fake_report()


def test_kind_root_cc_slices_main_scope(monkeypatch, tmp_path):
    """cc:--kind root 把主作用域切给 auditor,排除子 agent 内部;声明名忽略。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path, "wf")  # 声明名 "wf" ≠ 主 agent(root 忽略名)
    a_id = "call_A"
    records = [
        _asst("MAIN_TXT"),
        _tool_use("r1", "Read", {"file_path": "/tmp/app.log"}),
        _agent(a_id),  # 主发起 dispatch(主作用域)
        _in_sub(a_id, _asst("SUB_TXT")),  # 子 agent 内部(排除)
        _tool_result(a_id, "ok"),  # 主收 dispatch 返回(主作用域)
    ]
    tp = _write_jsonl(tmp_path / "s.jsonl", records)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(tp), "--kind", "root", "-o", str(out)])
    assert r.exit_code == 0, r.output

    assert len(fake.calls) == 1
    slices = fake.calls[0]["transcripts"]
    assert len(slices) == 1  # 主作用域单 span → 1 切片
    content = Path(slices[0]).read_text(encoding="utf-8")
    assert "MAIN_TXT" in content and "/tmp/app.log" in content
    assert "SUB_TXT" not in content  # 子 agent 内部排除

    # A1 旁路:被 root 切片排除的子代理痕迹作为 aux 传入(与 slices 同序;
    # 本 fixture 的子 agent 段只有文本无 tool_use → aux 为空列表但非 None)
    aux = fake.calls[0]["aux_tool_traces"]
    assert aux is not None and len(aux) == 1


def test_kind_root_opencode_uses_session_info_agent(monkeypatch, tmp_path):
    """opencode:--kind root 以 session.info.agent 为 target(复用 slice_opencode_by_agent)。"""
    from sift.evals.opencode import load_opencode_session

    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    p = Path("fixtures/example-skills/multi-skill-demo/multi-skill.json")
    main_agent = (load_opencode_session(p).get("info") or {}).get("agent")
    assert main_agent

    sp = _skill_dir(tmp_path, "wf")  # 声明名 "wf" ≠ main_agent(root 忽略名)
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(p), "--kind", "root", "-o", str(out)])
    assert r.exit_code == 0, r.output

    slices = fake.calls[0]["transcripts"]
    assert slices  # 主 agent 段非空
    for seg_path in slices:
        seg = json.loads(Path(seg_path).read_text(encoding="utf-8"))
        assert (seg.get("info") or {}).get("agent") == main_agent

    # A1 旁路:opencode 形按非主 agent message 提取(fixture 另两个 agent 有
    # read/write/bash 工具痕迹)→ aux 非空、执行者都是非主 agent;同一 tp 多段共享。
    aux = fake.calls[0]["aux_tool_traces"]
    assert aux is not None and len(aux) == len(slices)
    assert all(len(a) > 0 for a in aux)
    assert all(tc.agent != main_agent for tc in aux[0])
    assert {tc.name for tc in aux[0]} >= {"Read", "Write", "Bash"}  # 工具名与主 trace 同口径归一


def test_kind_root_structured_records_excludes_subagent(monkeypatch, tmp_path):
    """结构化 records 输入下 --kind root 切片正确排除子 agent(只含 parent_tool_use_id=null 的 root turn)。

    belt-and-suspenders:structured reader 已透传 parent_tool_use_id(structured.py L89/L92),
    _slice_root 按 parent_tool_use_id 为空过滤 → 此路径应直接 PASS;若红则 structured 透传断了。
    照 test_audit_structured.py 的 friendly JSON 构造模式({format:"sift-records", records}),
    1 root turn(parent_tool_use_id=null)+ 1 子 agent turn(带 parent_tool_use_id)。
    """
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    sp = _skill_dir(tmp_path, "wf")  # 声明名 "wf" ≠ 主 agent(root 忽略名)
    tp = tmp_path / "s.json"
    tp.write_text(
        json.dumps(
            {
                "format": "sift-records",
                "records": [
                    # root turn:主 agent 自己的 read(主作用域)
                    {
                        "role": "assistant",
                        "parent_tool_use_id": None,
                        "text": "MAIN_TXT",
                        "tool_calls": [{"name": "read", "input": {"file_path": "/tmp/main.md"}, "result": "m"}],
                    },
                    # 子 agent turn:parent_tool_use_id 非空 → 排除(归 kind="agent")
                    {
                        "role": "assistant",
                        "parent_tool_use_id": "tu_1",
                        "text": "SUB_TXT",
                        "tool_calls": [{"name": "read", "input": {"file_path": "/tmp/sub.md"}, "result": "s"}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(tp), "--kind", "root", "-o", str(out)])
    assert r.exit_code == 0, r.output

    assert len(fake.calls) == 1
    slices = fake.calls[0]["transcripts"]
    assert len(slices) == 1  # 主作用域单 span → 1 切片
    content = Path(slices[0]).read_text(encoding="utf-8")
    assert "MAIN_TXT" in content and "/tmp/main.md" in content
    assert "SUB_TXT" not in content  # 子 agent 内部排除

    # A1 旁路:被切片排除的子代理工具调用进 aux(角色名透传),供条件类第二遍匹配
    aux = fake.calls[0]["aux_tool_traces"]
    assert aux is not None and len(aux) == 1
    assert len(aux[0]) == 1  # 子 turn 的 1 个 read
    assert aux[0][0].name == "Read" and aux[0][0].input == {"file_path": "/tmp/sub.md"}


def test_kind_root_opencode_missing_info_agent_exits_2(monkeypatch, tmp_path):
    """opencode session 缺 info.agent → --kind root exit 2(无法定主 agent,不喂 auditor)。"""
    fake = _FakeAuditor()
    monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"info": {}, "messages": []}), encoding="utf-8")
    sp = _skill_dir(tmp_path, "wf")
    out = tmp_path / "out"
    r = invoke_cli(["audit", str(sp), "--transcript", str(bad), "--kind", "root", "-o", str(out)])
    assert r.exit_code == 2
    assert "info.agent" in r.output
    assert len(fake.calls) == 0  # 没喂 auditor(提前退出)


def test_kind_root_aux_locate_turns_passthrough(monkeypatch, tmp_path):
    """评审修复:build_subagent_tool_trace 的 locate_turns 随 CLI 透传——
    默认 --no-turn-refs 时 aux 记录 turn=None(与主 trace 同体系,evidence 不带
    无锚点可对的 (T{n}) 引用);--turn-refs 时带全局 turnIndex。
    """

    def _run(tag, extra):
        fake = _FakeAuditor()
        monkeypatch.setattr("sift.cli.commands.audit.TranscriptAuditor", lambda: fake)
        work = tmp_path / tag  # 两次调用各用独立子目录
        work.mkdir()
        sp = _skill_dir(work, "wf")
        a_id = "call_A"
        records = [
            _asst("MAIN_TXT"),
            _agent(a_id),
            _in_sub(a_id, _tool_use("s1", "Bash", {"command": "pytest -q"})),
            _tool_result(a_id, "ok"),
        ]
        tp = _write_jsonl(work / "s.jsonl", records)
        out = work / "out"
        r = invoke_cli(["audit", str(sp), "--transcript", str(tp), "--kind", "root", "-o", str(out)] + extra)
        assert r.exit_code == 0, r.output
        aux = fake.calls[0]["aux_tool_traces"]
        assert aux is not None and len(aux) == 1 and len(aux[0]) == 1
        return aux[0][0]

    rec_default = _run("default", [])  # 默认 --no-turn-refs
    assert rec_default.turn is None  # aux 与主 trace 一致:不带 turn 引用
    rec_turnrefs = _run("turnrefs", ["--turn-refs"])
    assert rec_turnrefs.turn is not None  # 开启时带全局 turnIndex
