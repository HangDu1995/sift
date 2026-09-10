#!/usr/bin/env python3
"""提交门禁的禁词检查（scripts/hooks/ 下的 hook 与 CI 调用）。

三个子命令：
- gate-staged：staged 新增行过禁词清单（git diff --cached）；
- message FILE：commit message 过禁词清单（先截剪刀线、跳 # 注释行——
  commit-msg 拿到的是清理前的原始文件）；
- gate-diff BASE：BASE...HEAD 的新增行过禁词清单（CI 查 PR 全量改动用，
  本地不调）。

清单两份、一起加载：banned-words.txt（公开，退役词→既定写法）与
banned-words-local.txt（可选，本地敏感词；内容不入库，文件在才生效）。

退出码：0 过 / 1 命中（阻断提交）/ 2 word_gate 自身出错。
只查新增行不扫全仓——存量动到哪行修哪行，与既定纪律一致。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_WORDS_FILES = [_SCRIPTS / "banned-words.txt", _SCRIPTS / "banned-words-local.txt"]
# 豁免：清单自己全是禁词；测试装着既定词反例
_EXEMPT = {"scripts/banned-words.txt", "scripts/banned-words-local.txt",
           "tests/test_word_gate.py"}
# commit -v 时剪刀线之后是不带 # 的 diff 正文，不截会把 staged 禁词再报一遍还归因错
_SCISSORS = "# ------------------------ >8 ------------------------"
_MAX_REPORT = 20


class GateError(Exception):
    """word_gate 自身出错（清单坏 / git 调用失败）——退出码 2。"""


def load_patterns() -> list[tuple[re.Pattern[str], str]]:
    """读禁词清单（模式<TAB>既定写法）；加载期逐行验正则，坏行报「第 N 行」。

    本地清单（banned-words-local.txt）可选——在才加载，不在只查公开清单。
    """
    out: list[tuple[re.Pattern[str], str]] = []
    for wf in _WORDS_FILES:
        try:
            text = wf.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as e:
            raise GateError(f"禁词清单读不了（{wf}）：{e}") from e
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.rstrip("\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" not in line:
                raise GateError(
                    f"{wf.name} 第 {n} 行没有制表符分隔（要 模式<TAB>既定写法）：{line[:40]!r}"
                )
            pat, hint = line.split("\t", 1)
            try:
                out.append((re.compile(pat), hint.strip()))
            except re.error as e:
                raise GateError(f"{wf.name} 第 {n} 行正则非法：{e}") from e
    return out


def parse_unified_diff(text: str) -> list[tuple[str, int, str]]:
    """unified diff → [(路径, 新文件行号, 新增行原文)]。

    逐行状态机、行首字面量判断，不赌 config：`+++ ` 切当前文件（/dev/null 当
    哨兵）、`@@` 的 +c 起算行号、`+` 是新增；上下文行也推进新文件行号。
    """
    out: list[tuple[str, int, str]] = []
    path = ""
    lineno = 0
    for raw in text.splitlines():
        ln = raw.rstrip("\r")
        if ln.startswith("+++ "):
            p = ln[4:].split("\t")[0].strip()
            path = "" if p == "/dev/null" else p
            lineno = 0
        elif ln.startswith("@@"):
            m = re.search(r"\+(\d+)", ln)
            lineno = int(m.group(1)) if m else 0
        elif not path:
            continue
        elif ln.startswith("+"):
            out.append((path, lineno, ln[1:]))
            lineno += 1
        elif ln.startswith(" "):
            lineno += 1
    return out


def _git(args: list[str]) -> str:
    """调 git（固定 --no-optional-locks，防读 diff 刷 index 撞 index.lock）。"""
    p = subprocess.run(
        ["git", "--no-optional-locks", *args], capture_output=True, check=False
    )
    if p.returncode != 0:
        why = p.stderr.decode("utf-8", "replace").strip()[:200]
        raise GateError(f"git {args[0]} 失败：{why}")
    return p.stdout.decode("utf-8", "replace")


def _fail(lines: list[str]) -> int:
    for ln in lines:
        print(ln, file=sys.stderr)
    return 1


def _scan_diff(diff: str) -> int:
    """unified diff 文本过禁词，命中打印报告、返回退出码。"""
    patterns = load_patterns()
    report: list[str] = []
    total = 0
    for path, lineno, line in parse_unified_diff(diff):
        rel = path[2:] if path.startswith("b/") else path
        if rel in _EXEMPT:
            continue
        for pat, hint in patterns:
            if pat.search(line):
                total += 1
                if len(report) < _MAX_REPORT:
                    report.append(f"{rel}:{lineno}: 禁词「{pat.pattern}」→ 既定写法「{hint}」")
    if not total:
        return 0
    if total > _MAX_REPORT:
        report.append(f"……另有 {total - _MAX_REPORT} 处未列出")
    report.append(
        f"共 {total} 处命中，提交被拦。换成既定写法；确认保留这条用词的，"
        "用 git commit --no-verify 提交（注意它会跳过全部检查）。"
    )
    return _fail(report)


def cmd_gate_staged() -> int:
    return _scan_diff(_git([
        "diff", "--cached",
        "--no-color", "--no-ext-diff", "--no-textconv",
        "--src-prefix=a/", "--dst-prefix=b/",
    ]))


def cmd_gate_diff(base: str) -> int:
    """CI 用：base...HEAD（merge-base 起）的新增行全量过禁词。"""
    return _scan_diff(_git([
        "diff", f"{base}...HEAD",
        "--no-color", "--no-ext-diff", "--no-textconv",
        "--src-prefix=a/", "--dst-prefix=b/",
    ]))


def cmd_message(msg_file: str) -> int:
    try:
        raw = Path(msg_file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise GateError(f"commit message 文件读不了（{msg_file}）：{e}") from e
    body = raw.split(_SCISSORS)[0]
    hits: list[tuple[int, str]] = []
    patterns = load_patterns()
    for i, ln in enumerate(body.splitlines(), 1):
        if ln.startswith("#"):
            continue
        for pat, hint in patterns:
            if pat.search(ln):
                hits.append((i, f"禁词「{pat.pattern}」→ 既定写法「{hint}」"))
    if not hits:
        return 0
    lines = [f"message 第 {i} 行: {msg}" for i, msg in hits[:_MAX_REPORT]]
    if len(hits) > _MAX_REPORT:
        lines.append(f"……另有 {len(hits) - _MAX_REPORT} 处未列出")
    lines.append(
        f"commit message 命中禁词 {len(hits)} 处，提交被拦。换成既定写法；"
        "确认保留这条用词的，用 git commit --no-verify 提交（注意它会跳过全部检查）。"
    )
    return _fail(lines)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "gate-staged":
        return cmd_gate_staged()
    if len(argv) == 3 and argv[1] == "message":
        return cmd_message(argv[2])
    if len(argv) == 3 and argv[1] == "gate-diff":
        return cmd_gate_diff(argv[2])
    print("用法：word_gate.py gate-staged | message FILE | gate-diff BASE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except GateError as e:
        print(f"word_gate 自身出错：{e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # 提交现场要人话不要裸 traceback
        print(f"word_gate 自身出错（{type(e).__name__}）：{e}——修它或 --no-verify", file=sys.stderr)
        sys.exit(2)
