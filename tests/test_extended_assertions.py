"""Tests for extended assertion types (cross_file_consistency, code_compiles) and plugin mechanism."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skill_eval.models import Assertion, AssertionType, EvalCase, ExpectationResult
from skill_eval.scoring.programmatic import ProgrammaticChecker
from skill_eval.scoring.plugins import AssertionPluginRegistry


@pytest.fixture
def checker() -> ProgrammaticChecker:
    return ProgrammaticChecker()


class TestCrossFileConsistency:
    def test_import_resolves_pass(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "utils.ts").write_text("export function helper() {}\n")
        (out / "main.ts").write_text("import { helper } from './utils'\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="imports resolve", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed

    def test_import_missing_fail(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "main.ts").write_text("import { helper } from './missing'\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="imports resolve", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
        ])
        results = checker.check(ec, out)
        assert not results[0].passed

    def test_no_imports_pass(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "standalone.ts").write_text("const x = 1\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="imports resolve", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed

    def test_include_filter(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "main.ts").write_text("import { x } from './missing'\n")
        (out / "notes.md").write_text("ignore this")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="only ts", type=AssertionType.CROSS_FILE_CONSISTENCY, params={"include": "*.ts"}),
        ])
        results = checker.check(ec, out)
        assert not results[0].passed

    def test_import_with_extension(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "utils.ts").write_text("export const X = 1\n")
        (out / "main.ts").write_text("import { X } from './utils.ts'\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="ext variant", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed


class TestCodeCompiles:
    def test_balanced_pass(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("function foo() { return 1; }\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="syntax", type=AssertionType.CODE_COMPILES, params={}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed

    def test_unbalanced_braces_fail(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("function foo() { return 1;\n")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="syntax", type=AssertionType.CODE_COMPILES, params={}),
        ])
        results = checker.check(ec, out)
        assert not results[0].passed

    def test_empty_dir_pass(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="syntax", type=AssertionType.CODE_COMPILES, params={}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed

    def test_include_filter(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "good.ts").write_text("const a = 1;\n")
        (out / "bad.txt").write_text("{{{")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="only ts", type=AssertionType.CODE_COMPILES, params={"include": "*.ts"}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed

    def test_mismatched_brackets_fail(self, checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("const arr = [1, 2);")
        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="syntax", type=AssertionType.CODE_COMPILES, params={}),
        ])
        results = checker.check(ec, out)
        assert not results[0].passed


class TestPluginRegistry:
    def test_register_and_use_custom_plugin(self, tmp_path: Path):
        registry = AssertionPluginRegistry()
        called = []

        def my_plugin(params: dict, output_dir: Path) -> tuple[bool, str]:
            called.append(True)
            return True, "custom plugin ran"

        registry.register("MY_CUSTOM_CHECK", my_plugin)

        checker = ProgrammaticChecker(plugin_registry=registry)
        out = tmp_path / "outputs"
        out.mkdir()

        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="custom", type=AssertionType.CUSTOM_PLUGIN, params={"plugin_type": "MY_CUSTOM_CHECK"}),
        ])
        results = checker.check(ec, out)
        assert results[0].passed
        assert called

    def test_unknown_plugin_fails(self, tmp_path: Path):
        registry = AssertionPluginRegistry()
        checker = ProgrammaticChecker(plugin_registry=registry)
        out = tmp_path / "outputs"
        out.mkdir()

        ec = EvalCase(id=1, prompt="test", assertions=[
            Assertion(name="missing", type=AssertionType.CUSTOM_PLUGIN, params={"plugin_type": "NONEXISTENT"}),
        ])
        results = checker.check(ec, out)
        assert not results[0].passed

    def test_register_multiple_plugins(self, tmp_path: Path):
        registry = AssertionPluginRegistry()
        registry.register("PLUGIN_A", lambda p, d: (True, "a"))
        registry.register("PLUGIN_B", lambda p, d: (False, "b"))
        assert "PLUGIN_A" in registry.list_plugins()
        assert "PLUGIN_B" in registry.list_plugins()
