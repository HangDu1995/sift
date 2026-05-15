"""Programmatic checker — tiered deterministic assertion evaluation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from skill_eval.models import (
    AssertionType,
    AssertionVerdict,
    EvalCase,
    ExpectationResult,
)

if TYPE_CHECKING:
    from skill_eval.models import ToolCallRecord
    from skill_eval.scoring.plugins import AssertionPluginRegistry


class ProgrammaticChecker:
    def __init__(self, plugin_registry: AssertionPluginRegistry | None = None) -> None:
        self._plugins = plugin_registry

    def check(
        self,
        eval_case: EvalCase,
        output_dir: Path,
        transcript: str = "",
        tool_trace: list | None = None,
        num_turns: int = 0,
        stop_reason: str = "",
    ) -> list[ExpectationResult]:
        results: list[ExpectationResult] = []
        context = {
            "transcript": transcript,
            "tool_trace": tool_trace or [],
            "num_turns": num_turns,
            "stop_reason": stop_reason,
        }
        for assertion in eval_case.assertions:
            verdict, evidence = self._run_assertion(
                assertion.type, assertion.params, output_dir, context
            )
            exp = ExpectationResult(
                text=assertion.name,
                passed=(verdict == AssertionVerdict.PASS),
                evidence=evidence,
                source="programmatic",
                verdict=verdict,
            )
            results.append(exp)
        return results

    def _run_assertion(
        self, atype: AssertionType, params: dict, output_dir: Path, ctx: dict
    ) -> tuple[AssertionVerdict, str]:
        handler = {
            # Tier 1
            AssertionType.FILE_EXISTS: self._file_exists,
            AssertionType.FILE_CONTAINS: self._file_contains,
            AssertionType.CONTAINS_PATTERN: self._contains_pattern,
            AssertionType.JSON_HAS_FIELD: self._json_has_field,
            AssertionType.LINE_COUNT: self._line_count,
            AssertionType.NOT_CONTAINS: self._not_contains,
            AssertionType.CUSTOM_SCRIPT: self._custom_script,
            AssertionType.CROSS_FILE_CONSISTENCY: self._cross_file_consistency,
            AssertionType.CODE_COMPILES: self._code_compiles,
            AssertionType.CUSTOM_PLUGIN: self._custom_plugin,
            AssertionType.TOOL_CALLED: self._tool_called,
            AssertionType.STEP_COUNT: self._step_count,
            # Tier 2
            AssertionType.GATE_REQUESTED: self._gate_requested,
            AssertionType.GATE_NOT_SKIPPED: self._gate_not_skipped,
            AssertionType.PHASE_ORDERED: self._phase_ordered,
            AssertionType.FILE_NOT_WRITTEN: self._file_not_written,
        }.get(atype)
        if handler is None:
            return AssertionVerdict.ERROR, f"Unknown assertion type: {atype}"
        return handler(params, output_dir, ctx)

    # ---- Tier 1 handlers ----

    def _file_exists(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        filename = params.get("filename", "")
        target = output_dir / filename
        if target.exists():
            return AssertionVerdict.PASS, f"File exists: {filename}"
        return AssertionVerdict.FAIL, f"File not found: {filename}"

    def _file_contains(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        filename = params.get("filename", "")
        pattern = params.get("pattern", "")
        target = output_dir / filename
        if not target.exists():
            return AssertionVerdict.FAIL, f"File not found: {filename}"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return AssertionVerdict.ERROR, f"Cannot read {filename}"
        if re.search(pattern, content):
            return AssertionVerdict.PASS, f"Pattern '{pattern}' found in {filename}"
        return AssertionVerdict.FAIL, f"Pattern '{pattern}' not found in {filename}"

    def _contains_pattern(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        pattern = params.get("pattern", "")
        filepath = params.get("path", "")
        target = output_dir / filepath if filepath else output_dir
        if target.is_file():
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(pattern, content):
                return AssertionVerdict.PASS, f"Pattern '{pattern}' found in {filepath or target}"
            return AssertionVerdict.FAIL, f"Pattern '{pattern}' not found in {filepath or target}"
        for f in output_dir.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if re.search(pattern, content):
                        return AssertionVerdict.PASS, f"Pattern '{pattern}' found in {f.name}"
                except Exception:
                    continue
        return AssertionVerdict.FAIL, f"Pattern '{pattern}' not found in any file"

    def _json_has_field(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        filename = params.get("filename", "")
        field = params.get("field", "")
        target = output_dir / filename
        if not target.exists():
            return AssertionVerdict.FAIL, f"File not found: {filename}"
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return AssertionVerdict.FAIL, f"Invalid JSON in {filename}"
        if self._has_nested_field(data, field):
            return AssertionVerdict.PASS, f"Field '{field}' found in {filename}"
        return AssertionVerdict.FAIL, f"Field '{field}' not found in {filename}"

    def _has_nested_field(self, data: dict, field: str) -> bool:
        parts = field.split(".")
        current = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return False
        return True

    def _line_count(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
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
            return AssertionVerdict.FAIL, f"Line count {count} < min {min_lines}"
        if max_lines is not None and count > max_lines:
            return AssertionVerdict.FAIL, f"Line count {count} > max {max_lines}"
        return AssertionVerdict.PASS, f"Line count: {count}"

    def _not_contains(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        pattern = params.get("pattern", "")
        filepath = params.get("path", "")
        target = output_dir / filepath if filepath else output_dir
        if target.is_file():
            content = target.read_text(encoding="utf-8", errors="replace")
            if re.search(pattern, content):
                return AssertionVerdict.FAIL, f"Pattern '{pattern}' found (should be absent)"
            return AssertionVerdict.PASS, f"Pattern '{pattern}' not found"

        for f in output_dir.rglob("*"):
            if f.is_file():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    if re.search(pattern, content):
                        return AssertionVerdict.FAIL, f"Pattern '{pattern}' found in {f.name} (should be absent)"
                except Exception:
                    continue
        return AssertionVerdict.PASS, f"Pattern '{pattern}' not found"

    def _custom_script(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        script = params.get("script", "")
        args = params.get("args", [])
        if not script:
            return AssertionVerdict.ERROR, "No script specified"
        try:
            result = subprocess.run(
                [sys.executable, script, *args],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                timeout=60,
            )
            if result.returncode == 0:
                return AssertionVerdict.PASS, f"Script passed: {result.stdout.strip()}"
            return AssertionVerdict.FAIL, f"Script failed (exit {result.returncode}): {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return AssertionVerdict.TIMEOUT, "Script timed out"
        except FileNotFoundError:
            return AssertionVerdict.FAIL, f"Script not found: {script}"

    def _cross_file_consistency(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        include = params.get("include")
        files = self._read_all_files(output_dir, include)
        if not files:
            return AssertionVerdict.PASS, "No files to check"
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
            return AssertionVerdict.FAIL, f"Unresolved imports: {broken[:5]}"
        return AssertionVerdict.PASS, "All imports resolve"

    def _code_compiles(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        include = params.get("include")
        files = self._read_all_files(output_dir, include)
        if not files:
            return AssertionVerdict.PASS, "No files to check"
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
            return AssertionVerdict.FAIL, f"Syntax issues: {errors[:5]}"
        return AssertionVerdict.PASS, "All files pass basic syntax check"

    def _custom_plugin(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        plugin_name = params.get("plugin_name") or params.get("plugin_type", "")
        if not self._plugins:
            return AssertionVerdict.ERROR, f"No plugin registry, cannot resolve '{plugin_name}'"
        handler = self._plugins.get(plugin_name)
        if handler is None:
            return AssertionVerdict.ERROR, f"Unknown plugin: '{plugin_name}'"
        passed, evidence = handler(params, output_dir)
        return (AssertionVerdict.PASS if passed else AssertionVerdict.FAIL), evidence

    def _tool_called(self, params: dict, _output_dir: Path, ctx: dict) -> tuple[AssertionVerdict, str]:
        tool_name = params.get("tool_name", "")
        min_count = params.get("min_count", 1)
        tool_trace = ctx.get("tool_trace", [])
        count = sum(1 for t in tool_trace if getattr(t, "name", "") == tool_name)
        if count >= min_count:
            return AssertionVerdict.PASS, f"Tool '{tool_name}' called {count} times (min {min_count})"
        return AssertionVerdict.FAIL, f"Tool '{tool_name}' called {count} times, need >= {min_count}"

    def _step_count(self, params: dict, _output_dir: Path, ctx: dict) -> tuple[AssertionVerdict, str]:
        num_turns = ctx.get("num_turns", 0)
        min_val = params.get("min")
        max_val = params.get("max")
        if min_val is not None and num_turns < min_val:
            return AssertionVerdict.FAIL, f"Step count {num_turns} < min {min_val}"
        if max_val is not None and num_turns > max_val:
            return AssertionVerdict.FAIL, f"Step count {num_turns} > max {max_val}"
        return AssertionVerdict.PASS, f"Step count: {num_turns}"

    # ---- Tier 2 handlers (use transcript context) ----

    def _gate_requested(self, params: dict, _output_dir: Path, ctx: dict) -> tuple[AssertionVerdict, str]:
        gate_label = params.get("gate", "")
        transcript = ctx.get("transcript", "")
        stop_reason = ctx.get("stop_reason", "")
        # Check if the agent asked the user for confirmation before proceeding
        ask_patterns = [
            r"(?:是否|请|could you|would you like|shall I|want me to).{0,30}",
            r"(?:确认|继续|审批|approve|continue|confirm|review).{0,20}[\?]",
        ]
        asked = any(re.search(p, transcript, re.IGNORECASE) for p in ask_patterns)
        stopped = "end_turn" in stop_reason.lower()
        if gate_label:
            if re.search(re.escape(gate_label), transcript, re.IGNORECASE):
                if asked or stopped:
                    return AssertionVerdict.PASS, f"Gate '{gate_label}' requested confirmation"
                return AssertionVerdict.PASS, f"Gate '{gate_label}' mentioned"
        if asked or stopped:
            return AssertionVerdict.PASS, "User confirmation requested"
        return AssertionVerdict.FAIL, "No confirmation request found"

    def _gate_not_skipped(self, params: dict, _output_dir: Path, ctx: dict) -> tuple[AssertionVerdict, str]:
        gate_label = params.get("gate", "")
        transcript = ctx.get("transcript", "")
        skip_patterns = [
            r"(?:跳过|skip(?:ping)?|bypass|ignore).{0,30}" + re.escape(gate_label),
            re.escape(gate_label) + r".{0,10}(?:跳过|skip|ignore|bypass)",
        ]
        for pattern in skip_patterns:
            if re.search(pattern, transcript, re.IGNORECASE):
                return AssertionVerdict.FAIL, f"Gate '{gate_label}' was skipped"
        return AssertionVerdict.PASS, f"Gate '{gate_label}' not skipped"

    def _phase_ordered(self, params: dict, _output_dir: Path, ctx: dict) -> tuple[AssertionVerdict, str]:
        phases = params.get("phases", [])
        if len(phases) < 2:
            return AssertionVerdict.ERROR, "Need at least 2 phases for ordering check"
        transcript = ctx.get("transcript", "")
        positions = {}
        for phase in phases:
            match = re.search(re.escape(phase), transcript, re.IGNORECASE)
            if match:
                positions[phase] = match.start()
            else:
                positions[phase] = float("inf")
        for i in range(len(phases) - 1):
            if positions.get(phases[i], float("inf")) > positions.get(phases[i + 1], float("inf")):
                return AssertionVerdict.FAIL, f"Phase '{phases[i]}' appears after '{phases[i+1]}'"
        return AssertionVerdict.PASS, f"Phases ordered correctly: {' → '.join(phases)}"

    def _file_not_written(self, params: dict, output_dir: Path, _ctx: dict) -> tuple[AssertionVerdict, str]:
        pattern = params.get("pattern", "*")
        import glob
        matched = glob.glob(str(output_dir / pattern), recursive=False)
        # Also check recursive
        matched.extend(glob.glob(str(output_dir / "**" / pattern), recursive=True))
        matched = list(set(matched))
        if matched:
            names = [Path(m).name for m in matched[:5]]
            return AssertionVerdict.FAIL, f"Forbidden files written: {', '.join(names)}"
        return AssertionVerdict.PASS, f"No files matching '{pattern}' written"

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
