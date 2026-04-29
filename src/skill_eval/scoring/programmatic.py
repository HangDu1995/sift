"""Programmatic checker — deterministic assertion evaluation."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from skill_eval.models import AssertionType, EvalCase, ExpectationResult, GradingResult, GradingSummary


class ProgrammaticChecker:
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


import sys
