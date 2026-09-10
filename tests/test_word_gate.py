# tests/test_word_gate.py
"""提交门禁(禁词清单)的单元测试。

word_gate.py 在 scripts/ 不在 src/,经 sys.path 直接 import(pyproject 的
pythonpath 只加 src)。真拦截行为的端到端(真 staged/真 message)靠
scripts/hooks/ 调用链,不在这里mock git。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import word_gate  # noqa: E402


def test_load_patterns_real_list_loads():
    """公开清单可加载:全部编译成 (Pattern, str),数量 sanity(首批 ≥10 条)。"""
    pats = word_gate.load_patterns()
    assert len(pats) >= 10
    assert all(isinstance(p, re.Pattern) and isinstance(h, str) and h for p, h in pats)


def test_load_patterns_local_list_optional(tmp_path, monkeypatch):
    """本地清单可选:在则并入加载,不在只查公开清单(不炸)。"""
    pub = tmp_path / "banned-words.txt"
    loc = tmp_path / "banned-words-local.txt"
    pub.write_text("公开词\t白话\n", encoding="utf-8")
    monkeypatch.setattr(word_gate, "_WORDS_FILES", [pub, loc])

    only_pub = word_gate.load_patterns()
    assert [p.pattern for p, _ in only_pub] == ["公开词"]

    loc.write_text("本地词\t禁用\n", encoding="utf-8")
    both = word_gate.load_patterns()
    assert [p.pattern for p, _ in both] == ["公开词", "本地词"]


def test_load_patterns_rejects_bad_lines(tmp_path, monkeypatch):
    """无制表符 / 正则非法的行报 GateError,带文件名与行号。"""
    pub = tmp_path / "banned-words.txt"
    loc = tmp_path / "banned-words-local.txt"
    loc.write_text("", encoding="utf-8")

    pub.write_text("没分隔符的一行\n", encoding="utf-8")
    monkeypatch.setattr(word_gate, "_WORDS_FILES", [pub, loc])
    with pytest.raises(word_gate.GateError, match="第 1 行没有制表符"):
        word_gate.load_patterns()

    pub.write_text("好词\t白话\n([坏正则\t提示\n", encoding="utf-8")
    with pytest.raises(word_gate.GateError, match="第 2 行正则非法"):
        word_gate.load_patterns()


def test_parse_unified_diff_paths_and_linenos():
    """unified diff 解析:新增行/路径/行号;删除文件(/dev/null)不产;上下文行推进行号。"""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " 保留行\n"
        "+新增一行\n"
        "+新增二行\n"
        "diff --git a/gone.py b/gone.py\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-整文件删除,这行不是新增\n"
    )
    got = word_gate.parse_unified_diff(diff)
    assert got == [("b/a.py", 2, "新增一行"), ("b/a.py", 3, "新增二行")]


def test_scan_diff_hits_and_exempt(capsys, monkeypatch):
    """命中 → 退出 1 + 报告(路径:行号 + 既定写法);豁免文件同词放行。"""
    monkeypatch.setattr(word_gate, "load_patterns",
                        lambda: [(re.compile("坏词"), "好词")])
    diff = (
        "--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1,1 +1,2 @@\n"
        " 上下文\n+这句里有坏词\n"
        "--- a/tests/test_word_gate.py\n+++ b/tests/test_word_gate.py\n@@ -1,0 +1,1 @@\n"
        "+豁免文件里的坏词(canary 反例)\n"
    )
    assert word_gate._scan_diff(diff) == 1
    err = capsys.readouterr().err
    assert "docs/x.md:2" in err and "「好词」" in err
    assert "test_word_gate" not in err          # 豁免不报


def test_cmd_message_scissors_and_comments(tmp_path, capsys):
    """message 检查:剪刀线之后的 diff 正文与 # 注释行不算;命中行报行号。"""
    banned_line = "这行有坏词"
    msg = tmp_path / "msg.txt"
    msg.write_text(
        f"subject\n\n{banned_line}\n"
        "# 注释里的坏词不算\n"
        "# ------------------------ >8 ------------------------\n"
        "剪刀线后 commit -v 的 diff 正文里的坏词不算\n",
        encoding="utf-8",
    )
    # 用真实清单跑:临时挂一个已知模式,免依赖清单具体内容
    orig = word_gate.load_patterns
    word_gate.load_patterns = lambda: [(re.compile("坏词"), "好词")]
    try:
        assert word_gate.cmd_message(str(msg)) == 1
    finally:
        word_gate.load_patterns = orig
    err = capsys.readouterr().err
    assert "第 3 行" in err
    assert err.count("message 第") == 1         # 只报非注释、剪刀线前那处(汇总行除外)


def test_main_usage_error():
    assert word_gate.main(["word_gate.py"]) == 2
    assert word_gate.main(["word_gate.py", "nonsense"]) == 2
