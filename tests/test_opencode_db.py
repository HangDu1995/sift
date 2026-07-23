# tests/test_opencode_db.py
"""opencode_db:从 opencode .db 文件 list/export session 的 plumbing 单测。

不真调 opencode(``subprocess.run`` 全 mock),验:
- ``_run_opencode``:env 注入 XDG_DATA_HOME、cwd=隔离目录、命令拼接、失败 → OpencodeDbError
- ``_isolated_xdg_env``:cp db 到 ``<tmp>/opencode/opencode.db``、退出清理、db 缺失报错
- ``list_sessions`` / ``export_session`` / ``resolve_session``(见各自 task 补的测)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import skill_eval.evals.opencode_db as ocdb
from skill_eval.errors import ExecutionError, OpencodeDbError


# ---- OpencodeDbError 层级 ----

def test_opencode_db_error_is_execution_error_exit3():
    assert issubclass(OpencodeDbError, ExecutionError)
    assert OpencodeDbError("x").exit_code == 3


# ---- _run_opencode ----

def test_run_opencode_passes_xdg_env_cwd_and_cmd(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="OK", stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    out = ocdb._run_opencode(tmp_path, ["session", "list", "--format", "json"])
    assert out == "OK"
    assert captured["cmd"] == ["opencode", "session", "list", "--format", "json"]
    # XDG_DATA_HOME 指向隔离目录(核心:opencode 凭此找到那个 db,不碰本机真库)
    assert captured["env"]["XDG_DATA_HOME"] == str(tmp_path)
    # cwd=隔离目录(非仓库)→ 规避 session list 的 cwd-scoping 过滤
    assert captured["cwd"] == str(tmp_path)


def test_run_opencode_nonzero_raises_with_stderr(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom!")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="boom!"):
        ocdb._run_opencode(tmp_path, ["export", "ses_x"])


def test_run_opencode_missing_binary_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="opencode"):
        ocdb._run_opencode(tmp_path, ["session", "list"])


def test_run_opencode_timeout_raises(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)
    with pytest.raises(OpencodeDbError, match="超时"):
        ocdb._run_opencode(tmp_path, ["export", "ses_x"], timeout=5)


# ---- _isolated_xdg_env ----

def test_isolated_xdg_env_copies_db_and_cleans_up(tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"SQLITE-CONTENT")

    with ocdb._isolated_xdg_env(db) as xdg:
        copied = xdg / "opencode" / "opencode.db"
        assert copied.exists()
        assert copied.read_bytes() == b"SQLITE-CONTENT"
        # 退出前目录还在
        assert xdg.exists()

    # 退出后临时目录清理干净
    assert not xdg.exists()


def test_isolated_xdg_env_missing_db_raises(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(OpencodeDbError, match="不存在"):
        with ocdb._isolated_xdg_env(missing):
            pass


# ---- list_sessions ----

_LIST_JSON = json.dumps([
    {"id": "ses_old", "title": "旧", "updated": 100, "created": 90, "projectId": "global"},
    {"id": "ses_new", "title": "新", "updated": 200, "created": 190, "projectId": "global"},
    {"id": "ses_mid", "title": "中", "updated": 150, "created": 140, "projectId": "global"},
])


def test_list_sessions_parses_and_sorts_desc_by_updated(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_LIST_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    sessions = ocdb.list_sessions(db)
    # 按 updated desc(新→旧)
    assert [s["id"] for s in sessions] == ["ses_new", "ses_mid", "ses_old"]
    # 命令是 session list --format json
    assert cmds == [["opencode", "session", "list", "--format", "json"]]


def test_list_sessions_empty_array_is_empty_list(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )
    assert ocdb.list_sessions(db) == []


def test_list_sessions_garbage_raises(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr=""),
    )
    with pytest.raises(OpencodeDbError, match="解析"):
        ocdb.list_sessions(db)


# ---- export_session ----

_EXPORT_JSON = json.dumps({"info": {"id": "ses_x", "agent": "build"}, "messages": []})


def test_export_session_default_no_sanitize_returns_json(monkeypatch, tmp_path):
    """默认不带 --sanitize:audit 靠 build_digest_from_opencode 读 part.text 判遵循性,
    --sanitize 会把文本脱敏成 [redacted:text:…] → 审计降级。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_EXPORT_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    out = ocdb.export_session(db, "ses_x")
    assert json.loads(out)["info"]["id"] == "ses_x"
    # 默认 export <id>,无 --sanitize
    assert cmds == [["opencode", "export", "ses_x"]]


def test_export_session_sanitize_opt_in(monkeypatch, tmp_path):
    """sanitize=True 才带 --sanitize(分享脱敏版;audit 不走这条)。"""
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=_EXPORT_JSON, stderr="")

    monkeypatch.setattr(ocdb.subprocess, "run", fake_run)

    ocdb.export_session(db, "ses_y", sanitize=True)
    assert cmds == [["opencode", "export", "ses_y", "--sanitize"]]


def test_export_session_non_session_shape_raises(monkeypatch, tmp_path):
    db = tmp_path / "handed.db"
    db.write_bytes(b"x")
    monkeypatch.setattr(
        ocdb.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout='{"not": "a session"}', stderr=""),
    )
    with pytest.raises(OpencodeDbError, match="合法"):
        ocdb.export_session(db, "ses_x")


# ---- resolve_session(纯策略,无 mock)----

def _sess(i: str, title: str = "t") -> dict:
    return {"id": i, "title": title, "updated": 0, "created": 0}


def test_resolve_session_id_match_returns_it():
    sessions = [_sess("ses_a"), _sess("ses_b")]
    assert ocdb.resolve_session(sessions, "ses_b")["id"] == "ses_b"


def test_resolve_session_id_not_found_lists_existing():
    sessions = [_sess("ses_a"), _sess("ses_b")]
    with pytest.raises(OpencodeDbError, match="ses_b") as exc:  # 错误信息含被找的 id
        ocdb.resolve_session(sessions, "ses_missing")
    assert "ses_a" in str(exc.value) and "ses_b" in str(exc.value)  # 且列出已有


def test_resolve_session_empty_raises():
    with pytest.raises(OpencodeDbError, match="无 session"):
        ocdb.resolve_session([], None)


def test_resolve_session_single_autopicks():
    sessions = [_sess("ses_only")]
    assert ocdb.resolve_session(sessions, None)["id"] == "ses_only"


def test_resolve_session_multi_returns_none():
    """多 session + 未指定 → 返回 None(信号歧义,交给 CLI 列清单 + exit 2)。"""
    sessions = [_sess("ses_a"), _sess("ses_b"), _sess("ses_c")]
    assert ocdb.resolve_session(sessions, None) is None
