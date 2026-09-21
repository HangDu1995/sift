"Tests for extended assertion types (cross_file_consistency, code_compiles) and plugin mechanism."

from __future__ import annotations

from pathlib import Path

import pytest

from sift.models import Assertion, AssertionType, EvalCase
from sift.scoring.plugins import AssertionPluginRegistry
from sift.scoring.programmatic import ExecutionEvidence, ProgrammaticChecker


@pytest.fixture
def checker() -> ProgrammaticChecker:
    return ProgrammaticChecker()


class TestCrossFileConsistency:
    @staticmethod
    def test_import_resolves_pass(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "utils.ts").write_text("export function helper() {}\n")
        (out / "main.ts").write_text("import { helper } from './utils'\n")
        ec = _import_consistency_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed

    @staticmethod
    def test_import_missing_fail(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "main.ts").write_text("import { helper } from './missing'\n")
        ec = _import_consistency_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert not results[0].passed

    @staticmethod
    def test_no_imports_pass(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "standalone.ts").write_text("const x = 1\n")
        ec = _import_consistency_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed

    @staticmethod
    def test_include_filter(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "main.ts").write_text("import { x } from './missing'\n")
        (out / "notes.md").write_text("ignore this")
        ec = EvalCase(
            id=1,
            prompt="test",
            assertions=[
                Assertion(name="only ts", type=AssertionType.CROSS_FILE_CONSISTENCY, params={"include": "*.ts"}),
            ],
        )
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert not results[0].passed

    @staticmethod
    def test_import_with_extension(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "utils.ts").write_text("export const X = 1\n")
        (out / "main.ts").write_text("import { X } from './utils.ts'\n")
        ec = EvalCase(
            id=1,
            prompt="test",
            assertions=[
                Assertion(name="ext variant", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
            ],
        )
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed


class TestCodeCompiles:
    @staticmethod
    def test_balanced_pass(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("function foo() { return 1; }\n")
        ec = _syntax_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed

    @staticmethod
    def test_unbalanced_braces_fail(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("function foo() { return 1;\n")
        ec = _syntax_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert not results[0].passed

    @staticmethod
    def test_empty_dir_pass(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        ec = _syntax_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed

    @staticmethod
    def test_include_filter(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "good.ts").write_text("const a = 1;\n")
        (out / "bad.txt").write_text("{{{")
        ec = EvalCase(
            id=1,
            prompt="test",
            assertions=[
                Assertion(name="only ts", type=AssertionType.CODE_COMPILES, params={"include": "*.ts"}),
            ],
        )
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed

    @staticmethod
    def test_mismatched_brackets_fail(checker: ProgrammaticChecker, tmp_path: Path):
        out = tmp_path / "outputs"
        out.mkdir()
        (out / "code.ts").write_text("const arr = [1, 2);")
        ec = _syntax_eval()
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert not results[0].passed


class TestPluginRegistry:
    @staticmethod
    def test_register_and_use_custom_plugin(tmp_path: Path):
        registry = AssertionPluginRegistry()
        called = []

        def my_plugin(params: dict, output_dir: Path) -> tuple[bool, str]:
            called.append(True)
            return True, "custom plugin ran"

        registry.register("MY_CUSTOM_CHECK", my_plugin)

        checker = ProgrammaticChecker(plugin_registry=registry)
        out = tmp_path / "outputs"
        out.mkdir()

        ec = EvalCase(
            id=1,
            prompt="test",
            assertions=[
                Assertion(name="custom", type=AssertionType.CUSTOM_PLUGIN, params={"plugin_type": "MY_CUSTOM_CHECK"}),
            ],
        )
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert results[0].passed
        assert called

    @staticmethod
    def test_unknown_plugin_fails(tmp_path: Path):
        registry = AssertionPluginRegistry()
        checker = ProgrammaticChecker(plugin_registry=registry)
        out = tmp_path / "outputs"
        out.mkdir()

        ec = EvalCase(
            id=1,
            prompt="test",
            assertions=[
                Assertion(name="missing", type=AssertionType.CUSTOM_PLUGIN, params={"plugin_type": "NONEXISTENT"}),
            ],
        )
        results = checker.check(eval_case=ec, output_dir=out, evidence=ExecutionEvidence())
        assert not results[0].passed

    @staticmethod
    def test_register_multiple_plugins(tmp_path: Path):
        registry = AssertionPluginRegistry()
        registry.register("PLUGIN_A", lambda p, d: (True, "a"))
        registry.register("PLUGIN_B", lambda p, d: (False, "b"))
        assert "PLUGIN_A" in registry.list_plugins()
        assert "PLUGIN_B" in registry.list_plugins()


def _import_consistency_eval():
    ec = EvalCase(
        id=1,
        prompt="test",
        assertions=[
            Assertion(name="imports resolve", type=AssertionType.CROSS_FILE_CONSISTENCY, params={}),
        ],
    )
    return ec


def _syntax_eval():
    ec = EvalCase(
        id=1,
        prompt="test",
        assertions=[
            Assertion(name="syntax", type=AssertionType.CODE_COMPILES, params={}),
        ],
    )
    return ec
