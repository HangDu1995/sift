"""Programmatic checker — deterministic assertion evaluation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from skill_eval.models import AssertionType, EvalCase, ExpectationResult, GradingResult, GradingSummary

if TYPE_CHECKING:
    from skill_eval.scoring.plugins import AssertionPluginRegistry


class ProgrammaticChecker:
    def __init__(self, plugin_registry: AssertionPluginRegistry | None = None) -> None:
        self._plugins = plugin_registry

    def check(self, eval_case: EvalCase, output_dir: Path) -> list[ExpectationResult]:
        results: list[ExpectationResult] = []
        for assertion in eval_case.assertions:
            passed, evidence = self._run_assertion(assertion.type, assertion.params, output_dir)
            results.append(ExpectationResult(
                text=assertion.name,
                passed=passed,
                evidence=evidence,
                source="programmatic",
            ))
        return results

    def _run_assertion(self, atype: AssertionType, params: dict, output_dir: Path) -> tuple[bool, str]:
        handler = {
            AssertionType.FILE_EXISTS: self._file_exists,
            AssertionType.CONTAINS_PATTERN: self._contains_pattern,
            AssertionType.JSON_HAS_FIELD: self._json_has_field,
            AssertionType.LINE_COUNT: self._line_count,
            AssertionType.NOT_CONTAINS: self._not_contains,
            AssertionType.CUSTOM_SCRIPT: self._custom_script,
            AssertionType.CROSS_FILE_CONSISTENCY: self._cross_file_consistency,
            AssertionType.CODE_COMPILES: self._code_compiles,
            AssertionType.CUSTOM_PLUGIN: self._custom_plugin,
        }.get(atype)
        if handler is None:
            return False, f"Unknown assertion type: {atype}"
        return handler(params, output_dir)

    def _file_exists(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        filename = params.get("filename", "")
        target = output_dir / filename
        if target.exists():
            return True, f"File exists: {filename}"
        return False, f"File not found: {filename}"

    def _contains_pattern(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        pattern = params.get("pattern", "")
        filepath = params.get("path", "")
        target = output_dir / filepath if filepath else output_dir
        if target.is_file():
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(pattern, content):
                return True, f"Pattern '{pattern}' found in {filepath or target}"
            return False, f"Pattern '{pattern}' not found in {filepath or target}"
        for f in output_dir.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if re.search(pattern, content):
                        return True, f"Pattern '{pattern}' found in {f.name}"
                except Exception:
                    continue
        return False, f"Pattern '{pattern}' not found in any file"

    def _json_has_field(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        filename = params.get("filename", "")
        field = params.get("field", "")
        target = output_dir / filename
        if not target.exists():
            return False, f"File not found: {filename}"
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False, f"Invalid JSON in {filename}"
        if self._has_nested_field(data, field):
            return True, f"Field '{field}' found in {filename}"
        return False, f"Field '{field}' not found in {filename}"

    def _has_nested_field(self, data: dict, field: str) -> bool:
        parts = field.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False
        return True

    def _line_count(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        min_lines = params.get("min")
        max_lines = params.get("max")
        filepath = params.get("path", "")
        target = output_dir / filepath if filepath else output_dir

        if target.is_file():
            count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
        else:
            total = 0
            for f in output_dir.rglob("*"):
                if f.is_file():
                    total += len(f.read_text(encoding="utf-8", errors="replace").splitlines())
            count = total

        if min_lines is not None and count < min_lines:
            return False, f"Line count {count} < min {min_lines}"
        if max_lines is not None and count > max_lines:
            return False, f"Line count {count} > max {max_lines}"
        return True, f"Line count: {count}"

    def _not_contains(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        pattern = params.get("pattern", "")
        filepath = params.get("path", "")
        target = output_dir / filepath if filepath else output_dir
        if target.is_file():
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(pattern, content):
                return False, f"Pattern '{pattern}' found (should be absent)"
            return True, f"Pattern '{pattern}' not found"

        for f in output_dir.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if re.search(pattern, content):
                        return False, f"Pattern '{pattern}' found in {f.name} (should be absent)"
                except Exception:
                    continue
        return True, f"Pattern '{pattern}' not found"

    def _custom_script(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        script = params.get("script", "")
        args = params.get("args", [])
        if not script:
            return False, "No script specified"
        try:
            result = subprocess.run(
                [sys.executable, script, *args],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                timeout=60,
            )
            if result.returncode == 0:
                return True, f"Script passed: {result.stdout.strip()}"
            return False, f"Script failed (exit {result.returncode}): {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, "Script timed out"
        except FileNotFoundError:
            return False, f"Script not found: {script}"

    def _cross_file_consistency(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        include = params.get("include")
        files = self._read_all_files(output_dir, include)
        if not files:
            return True, "No files to check"
        import_re = re.compile(r"""import\s+.*\s+from\s+['"](\.[^'"]+)['"]""")
        broken: list[str] = []
        for filepath, content in files.items():
            for m in import_re.finditer(content):
                ref = m.group(1)
                for ext in ("", ".ts", ".js", ".ets", ".json"):
                    if ext and ref.endswith(ext):
                        base = ref[:-len(ext)]
                    else:
                        base = ref
                    candidate = (Path(filepath).parent / (base + ext)).resolve()
                    if candidate.exists():
                        break
                else:
                    broken.append(f"{Path(filepath).name}: {ref}")
        if broken:
            return False, f"Unresolved imports: {broken[:5]}"
        return True, "All imports resolve"

    def _code_compiles(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        include = params.get("include")
        files = self._read_all_files(output_dir, include)
        if not files:
            return True, "No files to check"
        pairs = {"{": "}", "(": ")", "[": "]"}
        errors: list[str] = []
        for filepath, content in files.items():
            stack: list[str] = []
            for ch in content:
                if ch in pairs:
                    stack.append(ch)
                elif ch in pairs.values():
                    if not stack:
                        errors.append(f"{Path(filepath).name}: unexpected '{ch}'")
                        break
                    opener = stack.pop()
                    if pairs[opener] != ch:
                        errors.append(f"{Path(filepath).name}: mismatched '{opener}' vs '{ch}'")
                        break
            if stack and not errors:
                errors.append(f"{Path(filepath).name}: unclosed '{stack[-1]}'")
        if errors:
            return False, f"Syntax issues: {errors[:5]}"
        return True, "All files pass basic syntax check"

    def _custom_plugin(self, params: dict, output_dir: Path) -> tuple[bool, str]:
        plugin_name = params.get("plugin_name") or params.get("plugin_type", "")
        if not self._plugins:
            return False, f"No plugin registry, cannot resolve '{plugin_name}'"
        handler = self._plugins.get(plugin_name)
        if handler is None:
            return False, f"Unknown plugin: '{plugin_name}'"
        return handler(params, output_dir)

    @staticmethod
    def _read_all_files(output_dir: Path, include: str | None = None) -> dict[str, str]:
        contents: dict[str, str] = {}
        if include:
            pattern = str(output_dir / include)
        else:
            pattern = str(output_dir / "**" / "*")
        import glob
        for filepath in glob.glob(pattern, recursive=True):
            p = Path(filepath)
            if p.is_file():
                try:
                    contents[filepath] = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
        return contents
