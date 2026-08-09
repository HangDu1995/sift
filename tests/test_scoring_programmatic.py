"""Tests for programmatic checker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.models import Assertion, AssertionType, EvalCase
from skill_eval.scoring.programmatic import ProgrammaticChecker


@pytest.fixture
def checker() -> ProgrammaticChecker:
    return ProgrammaticChecker()


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "transform.py").write_text("import json\nimport csv\njson.dump(data, f)\n", encoding="utf-8")
    (out / "output.json").write_text(json.dumps({"name": "test", "count": 42}), encoding="utf-8")
    (out / "report.txt").write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    return out


class TestFileExists:
    def test_pass(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="exists", type=AssertionType.FILE_EXISTS, params={"filename": "transform.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert results[0].passed

    def test_fail(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="missing", type=AssertionType.FILE_EXISTS, params={"filename": "nonexistent.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert not results[0].passed


class TestContainsPattern:
    def test_pass(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="has json", type=AssertionType.CONTAINS_PATTERN, params={"pattern": "json.dump", "path": "transform.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert results[0].passed

    def test_fail(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="no xml", type=AssertionType.CONTAINS_PATTERN, params={"pattern": "xml.parse", "path": "transform.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert not results[0].passed


class TestJsonHasField:
    def test_pass(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="field", type=AssertionType.JSON_HAS_FIELD, params={"filename": "output.json", "field": "name"}),
        ])
        results = checker.check(ec, output_dir)
        assert results[0].passed

    def test_fail_missing_field(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="field", type=AssertionType.JSON_HAS_FIELD, params={"filename": "output.json", "field": "missing"}),
        ])
        results = checker.check(ec, output_dir)
        assert not results[0].passed


class TestLineCount:
    def test_pass(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="lines", type=AssertionType.LINE_COUNT, params={"min": 3, "max": 10, "path": "report.txt"}),
        ])
        results = checker.check(ec, output_dir)
        assert results[0].passed

    def test_fail_too_few(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="lines", type=AssertionType.LINE_COUNT, params={"min": 100, "path": "report.txt"}),
        ])
        results = checker.check(ec, output_dir)
        assert not results[0].passed


class TestNotContains:
    def test_pass(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="no eval", type=AssertionType.NOT_CONTAINS, params={"pattern": "eval\\(", "path": "transform.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert results[0].passed

    def test_fail(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="no json", type=AssertionType.NOT_CONTAINS, params={"pattern": "json", "path": "transform.py"}),
        ])
        results = checker.check(ec, output_dir)
        assert not results[0].passed


class TestScopeTranscript:
    """scope:'transcript' = 只查 agent/skill 的文本输出(transcript),完全不碰文件。

    只读/纯建议 agent(pr-reviewer 这类)不产文件,断言若带 path 指输入 fixture,
    _contains_pattern 会去读输入文件而非 agent 的回答 → 同义反复恒 PASS / 恒 FAIL。
    scope:'transcript' 把判卷对象从"文件"拨到"agent 的回答文本"。
    """

    def test_contains_pass_when_pattern_in_transcript(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="blocked", type=AssertionType.CONTAINS_PATTERN,
                      params={"pattern": "阻塞", "scope": "transcript"}),
        ])
        results = checker.check(ec, output_dir, transcript="review 标了 阻塞 不可合")
        assert results[0].passed

    def test_contains_ignores_files_when_scope_transcript(self, checker: ProgrammaticChecker, output_dir: Path):
        # json.dump 存在于 transform.py(文件里),但 transcript 没有 → 必 FAIL
        # 证明 scope:'transcript' 绕过文件搜索,不读输入/产物文件
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="json", type=AssertionType.CONTAINS_PATTERN,
                      params={"pattern": "json\\.dump", "scope": "transcript"}),
        ])
        results = checker.check(ec, output_dir, transcript="完全不相关的 review 文本")
        assert not results[0].passed

    def test_not_contains_pass_when_absent_from_transcript(self, checker: ProgrammaticChecker, output_dir: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="no nit", type=AssertionType.NOT_CONTAINS,
                      params={"pattern": "nitpick", "scope": "transcript"}),
        ])
        results = checker.check(ec, output_dir, transcript="干净的 review,无 nit")
        assert results[0].passed

    def test_not_contains_fail_when_present_in_transcript(self, checker: ProgrammaticChecker, output_dir: Path):
        # nitpick 在 transcript → FAIL(即便文件里没有)
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="no nit", type=AssertionType.NOT_CONTAINS,
                      params={"pattern": "nitpick", "scope": "transcript"}),
        ])
        results = checker.check(ec, output_dir, transcript="agent 开始 nitpick 命名风格")
        assert not results[0].passed


class TestNoAssertions:
    def test_empty(self, checker: ProgrammaticChecker, tmp_path: Path):
        ec = EvalCase(id=1, prompt="test", assertions=[])
        results = checker.check(ec, tmp_path)
        assert results == []
