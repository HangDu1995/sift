# tests/test_slice.py
import json
from pathlib import Path
from skill_eval.audit.slice import slice_transcript_by_skill

def _write(tmp_path: Path, recs: list) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    return p

def _skill(tid, name):
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Skill", "input": {"skill": name}}]}}

def _asst_text(t):
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}

def _tool_use(tid, name, inp):
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

def _tool_result(tid, text):
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}

def _agent(tid):
    """主作用域里发起子 agent 的 Agent tool_use。"""
    return {"message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": "Agent", "input": {"subagent_type": "general-purpose"}}]}}

def _in_sub(parent, rec):
    """给 record 打 parent_tool_use_id,归入子 agent 作用域。"""
    rec["parent_tool_use_id"] = parent
    return rec

def test_namespaced_skill_name_stripped_to_match(tmp_path):
    """真 transcript 的 input.skill 是命名空间名(如 plugin:skill),slicer 剥前缀后匹配 target。

    防护:所有别的测试都用裸名,若 rsplit 剥前缀逻辑坏了,全测试照绿但真 transcript
    (命名空间名)会静默得 0 span → 假 exit 2。这条钉死剥前缀路径。
    """
    recs = [
        _skill("s1", "ops-team:log-error-analyzer"),   # 命名空间名(非裸名)
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _tool_result("r1", "ERR"),
    ]
    spans = slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 1                              # 剥掉 ops-team: 前缀后命中
    assert any("app.log" in json.dumps(r, ensure_ascii=False) for r in spans[0])

def test_single_main_skill_span_to_end(tmp_path):
    recs = [
        _asst_text("pre-skill 主 agent 干的活,不该进段"),
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _tool_result("r1", "ERR boom"),
        _asst_text("classify + report"),
    ]
    spans = slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 1
    texts = [json.dumps(r, ensure_ascii=False) for r in spans[0]]
    assert any("log-error-analyzer" in t for t in texts)
    assert any("app.log" in t for t in texts)
    assert not any("pre-skill" in t for t in texts)


def test_main_multi_skill_interleaved_no_cross_contamination(tmp_path):
    recs = [
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _skill("s2", "code-review-auditor"),
        _tool_use("r2", "Read", {"file_path": "src/c.py"}),
        _skill("s3", "release-checklist-zh"),
        _tool_use("r3", "Bash", {"command": "git tag"}),
        _skill("s4", "log-error-analyzer"),
        _tool_use("r4", "Read", {"file_path": "app2.log"}),
    ]
    a = slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(a) == 2
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a for r in seg]
    assert any("app.log" in t for t in a_texts) and any("app2.log" in t for t in a_texts)
    assert not any("src/c.py" in t or "git tag" in t for t in a_texts)


def test_back_to_back_same_skill(tmp_path):
    recs = [
        _skill("s1", "log-error-analyzer"),
        _asst_text("first A work"),
        _skill("s2", "log-error-analyzer"),
        _asst_text("second A work"),
    ]
    spans = slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer")
    assert len(spans) == 2


def test_target_never_invoked_returns_empty(tmp_path):
    recs = [_skill("s1", "code-review-auditor"), _asst_text("x")]
    assert slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer") == []


def test_subagent_skill_scoped_to_return_no_main_pollution(tmp_path):
    agent_id = "call_agent1"
    recs = [
        _skill("s1", "log-error-analyzer"),
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": agent_id, "name": "Agent", "input": {"subagent_type": "general-purpose"}}]}},
        {"parent_tool_use_id": agent_id, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "sb", "name": "Skill", "input": {"skill": "code-review-auditor"}}]}},
        {"parent_tool_use_id": agent_id, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "rr", "name": "Read", "input": {"file_path": "src/c.py"}}]}},
        _tool_result(agent_id, "subagent done"),
        _tool_use("r2", "Read", {"file_path": "app2.log"}),
    ]
    a = slice_transcript_by_skill(_write(tmp_path, recs), "log-error-analyzer")
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a for r in seg]
    assert any("app2.log" in t for t in a_texts)      # 返回后主 agent 仍归 A(不被 B 污染)
    assert not any("src/c.py" in t for t in a_texts)   # 子 agent 内部不进主段
    b = slice_transcript_by_skill(_write(tmp_path, recs), "code-review-auditor")
    assert len(b) == 1
    b_texts = [json.dumps(r, ensure_ascii=False) for r in b[0]]
    assert any("src/c.py" in t for t in b_texts)


def test_nested_subagent_depth2_skill_scoped(tmp_path):
    """depth-2:main→A→B,B 内调 Skill;B 的段不泄漏到 main/A,main 的 skill 不受影响。"""
    a_id = "call_agentA"
    b_id = "call_agentB"
    recs = [
        _skill("s1", "log-error-analyzer"),                       # main 调 X
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _agent(a_id),                                            # main 起 A
        _in_sub(a_id, _agent(b_id)),                             # A 起 B
        _in_sub(b_id, _skill("sb", "code-review-auditor")),      # B 调 Y
        _in_sub(b_id, _tool_use("rb", "Read", {"file_path": "src/c.py"})),
        _in_sub(b_id, _asst_text("B analyze")),                  # B 干活
        _in_sub(a_id, _tool_result(b_id, "B done")),             # B 返回(在 A 作用域)
        _in_sub(a_id, _asst_text("A summarize")),                # A 干活
        _tool_result(a_id, "A done"),                            # A 返回(主作用域)
        _tool_use("r2", "Read", {"file_path": "app2.log"}),      # 主继续
    ]
    p = _write(tmp_path, recs)

    # (a) B 的 skill(code-review-auditor)段限定在 B 作用域
    b_spans = slice_transcript_by_skill(p, "code-review-auditor")
    assert len(b_spans) == 1
    b_texts = [json.dumps(r, ensure_ascii=False) for r in b_spans[0]]
    assert any("src/c.py" in t for t in b_texts)
    assert all(r.get("parent_tool_use_id") == b_id for r in b_spans[0])

    # (b) B 的活不泄漏到 main/A:target=log-error-analyzer 不含 src/c.py / B analyze
    a_spans = slice_transcript_by_skill(p, "log-error-analyzer")
    a_texts = [json.dumps(r, ensure_ascii=False) for seg in a_spans for r in seg]
    assert not any("src/c.py" in t or "B analyze" in t for t in a_texts)

    # (c) main 里调的 skill 不受影响:含 app.log / app2.log
    assert any("app.log" in t for t in a_texts)
    assert any("app2.log" in t for t in a_texts)


def test_same_skill_main_and_subagent_two_spans(tmp_path):
    """同 skill 在 main 和子 agent 各调一次→2 段(按作用域),互不污染。"""
    a_id = "call_agentA"
    recs = [
        _skill("s1", "log-error-analyzer"),                       # main 调 X
        _tool_use("r1", "Read", {"file_path": "app.log"}),
        _agent(a_id),                                            # main 起 A
        _in_sub(a_id, _skill("s2", "log-error-analyzer")),       # A 也调 X
        _in_sub(a_id, _tool_use("r2", "Read", {"file_path": "src/c.py"})),
        _in_sub(a_id, _asst_text("A analyze")),
        _tool_result(a_id, "A done"),                            # A 返回
        _tool_use("r3", "Read", {"file_path": "app2.log"}),      # main 继续
    ]
    p = _write(tmp_path, recs)
    spans = slice_transcript_by_skill(p, "log-error-analyzer")
    assert len(spans) == 2
    # 按 parent_tool_use_id 区分主段 / 子段(不依赖 spans 顺序)
    main_span = next(s for s in spans if {r.get("parent_tool_use_id") for r in s} == {None})
    sub_span = next(s for s in spans if {r.get("parent_tool_use_id") for r in s} == {a_id})

    main_texts = [json.dumps(r, ensure_ascii=False) for r in main_span]
    assert any("app.log" in t for t in main_texts)
    assert any("app2.log" in t for t in main_texts)
    assert not any("src/c.py" in t or "A analyze" in t for t in main_texts)

    sub_texts = [json.dumps(r, ensure_ascii=False) for r in sub_span]
    assert any("src/c.py" in t for t in sub_texts)
    assert not any("app.log" in t or "app2.log" in t for t in sub_texts)
