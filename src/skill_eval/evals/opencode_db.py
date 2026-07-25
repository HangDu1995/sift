"""opencode .db → 单 session export 的 plumbing:把「拿到一个 opencode .db 文件 → audit」
的手工三步(XDG_DATA_HOME 隔离 → ``opencode session list`` → ``opencode export`` 原文)
包成函数,供 audit 命令编排。

**不读 SQLite**:列 session 靠 ``opencode session list --format json``,导出靠
``opencode export <id>``(默认原文;``--sanitize`` 可选,但脱敏后别喂 audit ——
auditor 读 ``part.text`` 判遵循性);opencode schema 变是 opencode 的事,skill-eval 不背
SQLite 解析(与 b9ef5be「用真格式、不重造 export」决议一致)。

**实证(2026-07-25 探 ``~/.local/share/opencode/opencode.db``)**:库是**事件溯源**结构 ——
单表 ``event(id, aggregate_id, seq, type, data)``,按 ``aggregate_id``(=session)+ ``seq``
重放 6 种带版本后缀(``.1``)的内部事件(``message.part.updated`` / ``message.updated`` /
``session.*`` 等,单 session ~284 事件、part 增量更新要按 part_id 取末态)才能拼出 session。
直读 = 自己实现事件重放 + 绑 opencode 内部版本化 schema,维护锚重;且 read 输出在**源存储**
里就是 ``<content>``+``N: `` 格式(与 export 同),直读也绕不开清洗(见 ``opencode._clean_opencode_read_output``)。
唯一好处(无 opencode CLI 读别处拷来的 .db)场景窄,不值。

opencode ``export`` 无 ``--db`` 参数,但 opencode 认 ``XDG_DATA_HOME``(db 放
``$XDG_DATA_HOME/opencode/opencode.db``)→ 设该变量即可隔离指向任意 .db、不碰本机真库。

**cwd-scoping**:`opencode session list` 从 git 仓库 cwd 跑会按仓库过滤(列空),故子进程
cwd 必须是**非仓库目录** —— 这里用隔离临时目录本身作 cwd(它在 /tmp 下,非仓库)。

本模块纯逻辑(无 CLI/IO:不打表、不 ``typer.Exit``)。失败统一抛 ``OpencodeDbError``
(exit_code=3,被 audit 的 ``except SkillEvalError`` 兜住)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from skill_eval.errors import OpencodeDbError


def _run_opencode(xdg_dir: Path, args: list[str], timeout: int = 120) -> str:
    """在 ``XDG_DATA_HOME=xdg_dir``、``cwd=xdg_dir`` 下跑 ``opencode <args>``,返回 stdout。

    cwd 用隔离目录(非仓库)规避 ``session list`` 的 cwd-scoping 过滤。失败
    (binary 缺失 / 非零退出 / 超时)→ ``OpencodeDbError``。
    """
    cmd = ["opencode", *args]
    env = {**os.environ, "XDG_DATA_HOME": str(xdg_dir)}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(xdg_dir),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise OpencodeDbError(
            "opencode CLI 未找到;--db 需先装 opencode 并确保在 PATH"
            "(https://opencode.ai)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OpencodeDbError(
            f"opencode {' '.join(args)} 超时({timeout}s)"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise OpencodeDbError(
            f"opencode {' '.join(args)} 失败(exit {proc.returncode}): {detail[:500]}"
        )
    return proc.stdout


@contextmanager
def _isolated_xdg_env(db_path: Path):
    """搭隔离 XDG 环境:``mkdtemp`` → cp db_path 到 ``<tmp>/opencode/opencode.db`` → yield tmp。

    yield 的目录即作 ``XDG_DATA_HOME`` 和子进程 cwd(opencode 在此找到 db、且 cwd 非仓库
    保证 session list 列全)。退出 ``rmtree`` 清理。db 不存在 → ``OpencodeDbError``。
    """
    if not db_path.exists():
        raise OpencodeDbError(f"opencode db 文件不存在: {db_path}")
    tmp = Path(tempfile.mkdtemp(prefix="skill-eval-opencode-"))
    try:
        (tmp / "opencode").mkdir(parents=True)
        shutil.copy2(db_path, tmp / "opencode" / "opencode.db")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def _isolated_xdg_env_empty():
    """空隔离 XDG(写侧):mkdtemp → mkdir opencode/ → yield → rmtree。

    给 ``opencode run`` 用:会话落一次性 ``<tmp>/opencode/opencode.db``、不碰真库。
    与 ``_isolated_xdg_env(db_path)``(拷贝版,audit --db 读侧)对偶 —— 那个拷一个已有
    db 进去,这个建空目录让 opencode 自己写。
    """
    tmp = Path(tempfile.mkdtemp(prefix="skill-eval-opencode-run-"))
    try:
        (tmp / "opencode").mkdir(parents=True)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_sessions(db_path: Path) -> list[dict]:
    """列出 opencode db 里所有 session,按 ``updated`` desc(新→旧)返回。

    返回 ``opencode session list --format json`` 解析后的列表,每项形如
    ``{id, title, updated, created, projectId, directory}``。
    """
    with _isolated_xdg_env(db_path) as xdg:
        out = _run_opencode(xdg, ["session", "list", "--format", "json"])
    stripped = out.strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise OpencodeDbError(
            f"opencode session list 输出无法解析为 JSON: {stripped[:200]!r}"
        ) from exc
    if not isinstance(data, list):
        raise OpencodeDbError(
            f"opencode session list 输出非数组: {str(data)[:200]}"
        )
    return sorted(data, key=lambda s: s.get("updated", 0) or 0, reverse=True)


def export_session(db_path: Path, session_id: str, sanitize: bool = False) -> str:
    """导出单 session,返回 opencode export 格式 JSON 串(``{info, messages}``)。

    ``sanitize`` 默认 **False**(导原文):audit 靠 ``build_digest_from_opencode`` 读
    ``part.text`` 判遵循性,``--sanitize`` 会把文本脱敏成 ``[redacted:text:…]`` → auditor
    看不到任务/回复 → 审计降级(只剩工具骨架)。要脱敏分享请在 skill-eval 外手动
    ``opencode export --sanitize``。

    输出非合法 session JSON → ``OpencodeDbError``。
    """
    args = ["export", session_id]
    if sanitize:
        args.append("--sanitize")
    with _isolated_xdg_env(db_path) as xdg:
        out = _run_opencode(xdg, args)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise OpencodeDbError(
            f"opencode export 输出无法解析为 JSON(session={session_id}): {out[:200]!r}"
        ) from exc
    if not (isinstance(data, dict) and "messages" in data):
        raise OpencodeDbError(
            f"opencode export 输出非合法 session(缺 messages;session={session_id})"
        )
    return out


def resolve_session(sessions: list[dict], session_id: str | None) -> dict | None:
    """从 list_sessions 的结果里选出要审的 session(纯策略)。

    - ``session_id`` 给定 → 返回匹配项;不在列 → ``OpencodeDbError``(列出已有 id 帮用户挑)。
    - 0 条 → ``OpencodeDbError``("db 内无 session")。
    - 恰 1 条 → 自动返回它(用户无需 ``--session``)。
    - 多条 → 返回 ``None``(歧义信号:交给 CLI 打表 + exit 2,让用户带 ``--session`` 重跑)。
    """
    if session_id is not None:
        for s in sessions:
            if s.get("id") == session_id:
                return s
        have = ", ".join(s.get("id", "?") for s in sessions) or "(无)"
        raise OpencodeDbError(
            f"session {session_id} 不在此 db;现有: {have}"
        )
    if not sessions:
        raise OpencodeDbError("db 内无 session")
    if len(sessions) == 1:
        return sessions[0]
    return None
