"""Execution engine — ClaudeRunner and ABTestRunner."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable

from skill_eval.errors import ClaudeRunnerError
from skill_eval.models import (
    EvalCase,
    EvalSet,
    ExpectationResult,
    GradingResult,
    GradingSummary,
    RunConfig,
    RunOutput,
    ScoringConfig,
    TimingData,
)
from skill_eval.parser import parse_skill_md
from skill_eval.scoring.orchestrator import ScoringOrchestrator

ProgressCallback = Callable[..., None]


def find_project_root() -> Path:
    current = Path.cwd()
    for parent in [current, *current.parents]:
        if (parent / ".claude").is_dir():
            return parent
    return current


class ClaudeRunner:
    def run(
        self,
        prompt: str,
        skill_path: Path | None = None,
        model: str | None = None,
        timeout: int = 120,
        cwd: Path | None = None,
        max_turns: int = 10,
    ) -> RunOutput:
        project_root = cwd or find_project_root()
        command_file: Path | None = None

        try:
            if skill_path:
                name, description, content, _, _ = parse_skill_md(skill_path)
                unique_id = uuid.uuid4().hex[:8]
                clean_name = f"{name}-skill-{unique_id}"
                commands_dir = project_root / ".claude" / "commands"
                commands_dir.mkdir(parents=True, exist_ok=True)
                command_file = commands_dir / f"{clean_name}.md"

                indented_desc = "\n  ".join(description.split("\n"))
                command_content = (
                    f"---\n"
                    f"description: |\n"
                    f"  {indented_desc}\n"
                    f"---\n\n"
                    f"{content.split('---', 2)[-1].strip() if '---' in content else content}\n"
                )
                command_file.write_text(command_content, encoding="utf-8")

            cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions",
                    "--max-turns", str(max_turns)]
            if not skill_path:
                cmd.append("--bare")
            if model:
                cmd.extend(["--model", model])

            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            start = time.time()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(project_root),
                    env=env,
                )
                try:
                    stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise ClaudeRunnerError(f"claude -p timed out after {timeout}s (killed process tree)")
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                returncode = proc.returncode
            except ClaudeRunnerError:
                raise
            except FileNotFoundError:
                raise ClaudeRunnerError("'claude' CLI not found — ensure it is installed and in PATH")
            duration_ms = int((time.time() - start) * 1000)

            if returncode != 0:
                raise ClaudeRunnerError(f"claude -p exited with code {returncode}: {stderr[:500]}")

            transcript, total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace = self._parse_stream_json(stdout)

            return RunOutput(
                exit_code=returncode,
                stdout=stdout,
                duration_ms=duration_ms,
                total_tokens=total_tokens,
                transcript=transcript,
                tool_calls=tool_calls,
                tool_trace=tool_trace,
                result_text=result_text,
                num_turns=num_turns,
                stop_reason=stop_reason,
            )
        except subprocess.TimeoutExpired:
            raise ClaudeRunnerError(f"claude -p timed out after {timeout}s")
        finally:
            if command_file and command_file.exists():
                command_file.unlink()

    def _parse_stream_json(self, stdout: str) -> tuple[str, int, dict[str, int], str, int, str, list]:
        from skill_eval.models import ToolCallRecord
        transcript_parts: list[str] = []
        total_tokens = 0
        tool_calls: dict[str, int] = {}
        result_text = ""
        num_turns = 0
        stop_reason = ""
        tool_trace: list[ToolCallRecord] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "result":
                total_tokens = event.get("total_tokens", total_tokens) or total_tokens
                result_text = event.get("result", "") or result_text
                num_turns = event.get("num_turns", num_turns)
                stop_reason = event.get("stop_reason", stop_reason) or event.get("subtype", "")

            elif event.get("type") == "assistant":
                message = event.get("message", {})
                for content_item in message.get("content", []):
                    if content_item.get("type") == "text":
                        transcript_parts.append(content_item.get("text", ""))
                    elif content_item.get("type") == "tool_use":
                        tool_name = content_item.get("name", "")
                        tool_calls[tool_name] = tool_calls.get(tool_name, 0) + 1
                        tool_input = content_item.get("input", {})
                        input_summary = json.dumps(tool_input, ensure_ascii=False)[:200] if tool_input else ""
                        tool_trace.append(ToolCallRecord(name=tool_name, input_summary=input_summary))

        return "\n".join(transcript_parts), total_tokens, tool_calls, result_text, num_turns, stop_reason, tool_trace


class ABTestRunner:
    def __init__(
        self,
        config: RunConfig,
        scoring_config: ScoringConfig,
        on_progress: ProgressCallback | None = None,
    ):
        self.config = config
        self.scoring_config = scoring_config
        self.runner = ClaudeRunner()
        self.on_progress = on_progress

    def run_ab_test(self, skill_path: Path, eval_set: EvalSet) -> Path:
        output_dir = self.config.output_dir
        iteration_dir = output_dir / "iteration-1"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        merged_prompt = self._build_merged_prompt(eval_set)
        num_checkpoints = len(eval_set.evals)

        (iteration_dir / "merged_prompt.txt").write_text(merged_prompt, encoding="utf-8")

        total_runs = 2 * self.config.runs_per_config
        completed_runs = 0

        for config_name in ("with_skill", "without_skill"):
            config_dir = iteration_dir / config_name
            config_dir.mkdir(exist_ok=True)

            for run_num in range(1, self.config.runs_per_config + 1):
                run_dir = config_dir / f"run-{run_num}"
                run_dir.mkdir(exist_ok=True)
                outputs_dir = run_dir / "outputs"
                outputs_dir.mkdir(exist_ok=True)

                for eval_case in eval_set.evals:
                    eval_case.setup_fixtures(outputs_dir)

                run_start = time.time()
                self._emit_progress(
                    config_name, config_name,
                    1, 1,
                    completed_runs, total_runs,
                    None, 0.0, None, phase="executing",
                )

                use_skill = skill_path if config_name == "with_skill" else None
                try:
                    run_output = self.runner.run(
                        prompt=merged_prompt,
                        skill_path=use_skill,
                        model=self.config.model,
                        timeout=self.config.timeout_seconds,
                        max_turns=self.config.max_turns,
                    )
                except ClaudeRunnerError as exc:
                    run_duration = time.time() - run_start
                    (run_dir / "error.txt").write_text(str(exc), encoding="utf-8")
                    grading = GradingResult(
                        summary=GradingSummary(pass_rate=0.0, passed=0, failed=0, total=0),
                        expectations=[],
                        evidence=f"Run failed: {exc}",
                    )
                    (run_dir / "grading.json").write_text(
                        grading.model_dump_json(indent=2), encoding="utf-8"
                    )
                    completed_runs += 1
                    self._emit_progress(
                        config_name, config_name,
                        1, 1,
                        completed_runs, total_runs,
                        grading, run_duration, None,
                    )
                    continue

                timing = TimingData(
                    total_tokens=run_output.total_tokens,
                    duration_ms=run_output.duration_ms,
                    total_duration_seconds=run_output.duration_ms / 1000.0,
                )
                (run_dir / "timing.json").write_text(
                    timing.model_dump_json(indent=2), encoding="utf-8"
                )

                self._emit_progress(
                    config_name, config_name,
                    1, 1,
                    completed_runs, total_runs,
                    None, 0.0, None, phase="scoring",
                    substep=f"batch checking {num_checkpoints} checkpoints",
                )

                all_expectations, per_checkpoint = self._score_checkpoints(
                    eval_set.evals, run_output.transcript, outputs_dir,
                )

                passed = sum(1 for e in all_expectations if e.passed)
                total_exp = len(all_expectations)
                overall_grading = GradingResult(
                    expectations=all_expectations,
                    summary=GradingSummary(
                        passed=passed,
                        failed=total_exp - passed,
                        total=total_exp,
                        pass_rate=passed / total_exp if total_exp > 0 else 0.0,
                    ),
                )
                (run_dir / "grading.json").write_text(
                    overall_grading.model_dump_json(indent=2), encoding="utf-8"
                )
                (run_dir / "checkpoints.json").write_text(
                    json.dumps(per_checkpoint, indent=2, ensure_ascii=False), encoding="utf-8"
                )

                compliance_report = None

                if config_name == "with_skill" and skill_path:
                    self._emit_progress(
                        config_name, config_name,
                        1, 1,
                        completed_runs, total_runs,
                        None, 0.0, None, phase="compliance",
                    )
                    from skill_eval.compliance.extractor import InstructionExtractor
                    from skill_eval.compliance.checker import ComplianceChecker

                    extractor = InstructionExtractor()
                    _, _, _, _, skill_body = parse_skill_md(skill_path)
                    instructions = extractor.extract(skill_body)

                    if instructions:
                        log_path = run_dir / "compliance.log"
                        log_path.write_text("", encoding="utf-8")

                        def _on_compliance_substep(step, total_instr, text, followed, confidence):
                            self._emit_progress(
                                config_name, config_name,
                                1, 1,
                                completed_runs, total_runs,
                                None, 0.0, None, phase="compliance",
                                substep=f"checking {step}/{total_instr}",
                            )

                        checker = ComplianceChecker()
                        compliance_report = checker.check(
                            instructions=instructions,
                            transcript=run_output.transcript,
                            output_dir=outputs_dir,
                            grading=overall_grading,
                            model=self.config.model,
                            timeout=self.scoring_config.llm_timeout,
                            tool_trace=list(run_output.tool_trace),
                            on_substep=_on_compliance_substep,
                            log_path=log_path,
                        )
                        compliance_report.skill_name = eval_set.skill_name
                        (run_dir / "compliance.json").write_text(
                            compliance_report.model_dump_json(indent=2), encoding="utf-8"
                        )

                self._emit_progress(
                    config_name, config_name,
                    1, 1,
                    completed_runs, total_runs,
                    None, 0.0, None, phase="calibration",
                )
                from skill_eval.calibration.calibrator import CalibrationChecker

                calibrator = CalibrationChecker()
                calibration_report = calibrator.calibrate(overall_grading, compliance=compliance_report)
                (run_dir / "calibration.json").write_text(
                    calibration_report.model_dump_json(indent=2), encoding="utf-8"
                )

                run_debug = {
                    "config": config_name,
                    "run_num": run_num,
                    "prompt": merged_prompt[:500],
                    "duration_ms": run_output.duration_ms,
                    "total_tokens": run_output.total_tokens,
                    "num_turns": run_output.num_turns,
                    "stop_reason": run_output.stop_reason,
                    "tool_calls": run_output.tool_calls,
                    "tool_trace": [t.model_dump() for t in run_output.tool_trace],
                    "result_text": run_output.result_text[:2000] if run_output.result_text else "",
                    "transcript_preview": run_output.transcript[:2000] if run_output.transcript else "",
                    "grading_summary": overall_grading.summary.model_dump(),
                    "num_checkpoints": num_checkpoints,
                    "calibration_grade": calibration_report.grade.value,
                    "calibration_confidence": calibration_report.overall_confidence,
                    "calibration_agreement_rate": calibration_report.agreement_rate,
                }
                if compliance_report:
                    run_debug["compliance_rate"] = compliance_report.compliance_rate
                (run_dir / "run_debug.json").write_text(
                    json.dumps(run_debug, indent=2, ensure_ascii=False), encoding="utf-8"
                )

                completed_runs += 1
                run_duration = time.time() - run_start
                self._emit_progress(
                    config_name, config_name,
                    1, 1,
                    completed_runs, total_runs,
                    overall_grading, run_duration, run_output,
                )

        return iteration_dir

    @staticmethod
    def _build_merged_prompt(eval_set: EvalSet) -> str:
        sections = []
        for eval_case in eval_set.evals:
            sections.append(f"## {eval_case.get_name()}\n{eval_case.prompt}")
        return (
            f"You are evaluating the skill: {eval_set.skill_name}\n\n"
            f"Please complete ALL of the following tasks in sequence:\n\n"
            + "\n\n".join(sections)
        )

    def _score_checkpoints(
        self,
        eval_cases: list[EvalCase],
        transcript: str,
        outputs_dir: Path,
    ) -> tuple[list, list[dict]]:
        prog_checker = None
        if self.scoring_config.enable_programmatic:
            from skill_eval.scoring.programmatic import ProgrammaticChecker
            prog_checker = ProgrammaticChecker()

        per_checkpoint = []
        all_expectations = []
        checkpoint_list = ""
        expectation_map: list[tuple[int, str]] = []
        prog_results_by_cp: dict[int, list] = {}

        for cp_idx, eval_case in enumerate(eval_cases):
            for exp_text in eval_case.expectations:
                expectation_map.append((cp_idx, exp_text))

            if eval_case.assertions and prog_checker:
                cp_prog = prog_checker.check(eval_case, outputs_dir)
                all_expectations.extend(cp_prog)
                prog_results_by_cp[cp_idx] = cp_prog

            checkpoint_list += f"\n### Checkpoint {cp_idx}: {eval_case.get_name()}\n"
            for i, exp in enumerate(eval_case.expectations):
                checkpoint_list += f"  [{len(expectation_map) - len(eval_case.expectations) + i}] {exp}\n"

        if not expectation_map:
            return all_expectations, per_checkpoint

        prompt = (
            f"You are a checkpoint evaluator. Determine which expectations are covered in the execution transcript.\n\n"
            f"## Expectations ({len(expectation_map)} total across {len(eval_cases)} checkpoints)\n"
            f"{checkpoint_list}\n\n"
            f"## Transcript\n{transcript[:12000]}\n\n"
            f"## Output Format\n\n"
            f"Respond with ONLY a JSON array:\n"
            f'[{{"index": 0, "passed": true, "evidence": "brief reason"}}]\n\n'
            f"Judge each independently. passed=true only if there is clear evidence in the transcript."
        )

        from skill_eval.scoring.llm_grader import LLMGrader
        grader = LLMGrader(model=self.scoring_config.llm_model, timeout=self.scoring_config.llm_timeout)
        response = grader._call_claude(prompt)

        results_by_index: dict[int, tuple[bool, str]] = {}
        import re
        array_match = re.search(r"\[[\s\S]*\]", response)
        if array_match:
            try:
                import json as _json
                items = _json.loads(array_match.group())
                for item in items:
                    if isinstance(item, dict):
                        idx = item.get("index")
                        if isinstance(idx, int):
                            results_by_index[idx] = (
                                bool(item.get("passed", False)),
                                str(item.get("evidence", "")),
                            )
            except Exception:
                pass

        exp_results_by_cp: dict[int, list] = {}
        for global_idx, (cp_idx, exp_text) in enumerate(expectation_map):
            passed, evidence = results_by_index.get(global_idx, (False, "Not evaluated"))
            exp_result = ExpectationResult(
                text=exp_text, passed=passed, evidence=evidence, source="llm_batch",
            )
            all_expectations.append(exp_result)
            exp_results_by_cp.setdefault(cp_idx, []).append(exp_result)

        for cp_idx, eval_case in enumerate(eval_cases):
            cp_exps = exp_results_by_cp.get(cp_idx, [])
            cp_assertions = prog_results_by_cp.get(cp_idx, [])
            cp_all = cp_assertions + cp_exps
            cp_passed = sum(1 for e in cp_all if e.passed)
            cp_total = len(cp_all)
            per_checkpoint.append({
                "eval_id": eval_case.id,
                "eval_name": eval_case.get_name(),
                "passed": cp_passed,
                "failed": cp_total - cp_passed,
                "total": cp_total,
                "pass_rate": cp_passed / cp_total if cp_total > 0 else 0.0,
                "assertions": [
                    {"text": e.text, "passed": e.passed, "evidence": e.evidence, "source": e.source}
                    for e in cp_assertions
                ],
                "expectations": [
                    {"text": e.text, "passed": e.passed, "evidence": e.evidence, "source": e.source}
                    for e in cp_exps
                ],
            })

        return all_expectations, per_checkpoint

    def _emit_progress(
        self,
        eval_name: str,
        config_name: str,
        eval_idx: int,
        total_evals: int,
        completed: int,
        total: int,
        grading: GradingResult | None,
        run_duration: float = 0.0,
        run_output: RunOutput | None = None,
        phase: str = "",
        substep: str = "",
    ):
        if self.on_progress:
            self.on_progress(
                eval_name, config_name,
                eval_idx, total_evals,
                completed, total,
                grading, run_duration, run_output, phase=phase, substep=substep,
            )
