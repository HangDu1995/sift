"""Execution engine — ABTestRunner (orchestrates A/B evaluations).

ClaudeRunner lives in cli_runner.py. Anomaly detection lives in anomaly.py.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from skill_eval.errors import ClaudeRunnerError
from skill_eval.execution.cli_runner import ClaudeRunner, find_project_root  # noqa: F401 — re-exported for backward compat
from skill_eval.execution.anomaly import detect_outliers, detect_timeout  # noqa: F401 — re-exported for backward compat
from skill_eval.models import (
    AssertionVerdict,
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
from skill_eval.scoring.programmatic import ProgrammaticChecker

ProgressCallback = Callable[..., None]


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
        evals_to_run = self._sample_evals(eval_set.evals)

        if self.config.per_eval_mode and len(evals_to_run) > 1:
            return self._run_per_eval(skill_path, eval_set, evals_to_run)

        return self._run_merged(skill_path, eval_set, evals_to_run)

    def _sample_evals(self, evals: list[EvalCase]) -> list[EvalCase]:
        if not self.config.eval_samples:
            return evals
        n = len(evals)
        if isinstance(self.config.eval_samples, float) and self.config.eval_samples <= 1.0:
            count = max(1, int(n * self.config.eval_samples))
        else:
            count = min(int(self.config.eval_samples), n)
        import random
        return random.sample(evals, count)

    def _run_per_eval(self, skill_path: Path, eval_set: EvalSet, evals: list[EvalCase]) -> Path:
        output_dir = self.config.output_dir
        iteration_dir = output_dir / "iteration-1"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        total_jobs = len(evals) * 2 * self.config.runs_per_config
        completed = [0]
        lock = threading.Lock()

        max_retries = getattr(self.config, "max_retries_per_eval", 0)

        def run_single_eval(eval_case: EvalCase, config_name: str, run_num: int, retry_num: int = 0) -> str:
            run_label = f"{eval_case.get_name()}::{config_name}::run-{run_num}"
            if retry_num > 0:
                run_label += f"::retry-{retry_num}"
            config_dir = iteration_dir / config_name / f"eval-{eval_case.id}" / f"run-{run_num}"
            if retry_num > 0:
                config_dir = iteration_dir / config_name / f"eval-{eval_case.id}" / f"run-{run_num}-retry-{retry_num}"
            config_dir.mkdir(parents=True, exist_ok=True)

            grading_path = config_dir / "grading.json"
            if retry_num == 0 and grading_path.exists():
                try:
                    existing = json.loads(grading_path.read_text(encoding="utf-8"))
                    if existing.get("summary", {}).get("total", 0) > 0:
                        with lock:
                            completed[0] += 1
                        self._emit_progress(
                            eval_case.get_name(), config_name,
                            eval_case.id, len(evals),
                            completed[0], total_jobs,
                            GradingResult.model_validate(existing),
                            0.0, None, phase="skipped",
                            substep="already completed",
                        )
                        return run_label
                except Exception:
                    pass
            outputs_dir = config_dir / "outputs"
            outputs_dir.mkdir(exist_ok=True)
            eval_case.setup_fixtures(outputs_dir)

            run_start = time.time()
            use_skill = skill_path if config_name == "with_skill" else None
            prompt = eval_case.prompt
            if eval_case.expected_output:
                prompt += f"\n\nExpected output:\n{eval_case.expected_output}"

            self._emit_progress(
                eval_case.get_name(), config_name,
                eval_case.id, len(evals),
                completed[0], total_jobs,
                None, 0.0, None, phase="executing",
            )

            try:
                run_output = self.runner.run(
                    prompt=prompt,
                    skill_path=use_skill,
                    model=self.config.model,
                    timeout=self.config.timeout_seconds,
                    max_turns=self.config.max_turns,
                    cwd=outputs_dir,
                )
            except ClaudeRunnerError as exc:
                run_duration = time.time() - run_start
                (config_dir / "error.txt").write_text(str(exc), encoding="utf-8")
                grading = GradingResult(
                    summary=GradingSummary(pass_rate=0.0, passed=0, failed=0, total=0),
                    expectations=[ExpectationResult(text=e, passed=False, evidence=str(exc), source="error")
                                  for e in eval_case.expectations],
                    error=str(exc),
                )
                (config_dir / "grading.json").write_text(grading.model_dump_json(indent=2), encoding="utf-8")
                if retry_num < max_retries:
                    shutil.rmtree(config_dir, ignore_errors=True)
                    self._emit_progress(
                        eval_case.get_name(), config_name,
                        eval_case.id, len(evals),
                        completed[0], total_jobs,
                        grading, run_duration, None,
                        phase="retrying",
                        substep=f"error, retry {retry_num+1}/{max_retries}",
                    )
                    return run_single_eval(eval_case, config_name, run_num, retry_num + 1)
                with lock:
                    completed[0] += 1
                self._emit_progress(
                    eval_case.get_name(), config_name,
                    eval_case.id, len(evals),
                    completed[0], total_jobs,
                    grading, run_duration, None,
                    phase="failed",
                )
                return run_label

            timing = TimingData(
                total_tokens=run_output.total_tokens,
                duration_ms=run_output.duration_ms,
                total_duration_seconds=run_output.duration_ms / 1000.0,
            )
            (config_dir / "timing.json").write_text(timing.model_dump_json(indent=2), encoding="utf-8")

            self._emit_progress(
                eval_case.get_name(), config_name,
                eval_case.id, len(evals),
                completed[0], total_jobs,
                None, 0.0, None, phase="scoring",
            )

            all_expectations, per_checkpoint = self._score_checkpoints(
                [eval_case], run_output.transcript, outputs_dir,
                tool_trace=run_output.tool_trace,
                num_turns=run_output.num_turns,
                stop_reason=run_output.stop_reason,
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
            (config_dir / "grading.json").write_text(overall_grading.model_dump_json(indent=2), encoding="utf-8")
            (config_dir / "checkpoints.json").write_text(
                json.dumps(per_checkpoint, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            compliance_report = None
            if config_name == "with_skill" and skill_path:
                self._emit_progress(
                    eval_case.get_name(), config_name,
                    eval_case.id, len(evals),
                    completed[0], total_jobs,
                    None, 0.0, None, phase="compliance",
                )
                from skill_eval.compliance.extractor import InstructionExtractor
                from skill_eval.compliance.checker import ComplianceChecker

                extractor = InstructionExtractor()
                _, _, _, _, skill_body = parse_skill_md(skill_path)
                instructions = extractor.extract(skill_body)
                if instructions:
                    log_path = config_dir / "compliance.log"
                    log_path.write_text("", encoding="utf-8")
                    checker = ComplianceChecker()
                    compliance_report = checker.check(
                        instructions=instructions,
                        transcript=run_output.transcript,
                        output_dir=outputs_dir,
                        model=self.config.model,
                        timeout=self.scoring_config.llm_timeout,
                        tool_trace=list(run_output.tool_trace),
                        log_path=log_path,
                    )
                    compliance_report.skill_name = eval_set.skill_name
                    (config_dir / "compliance.json").write_text(
                        compliance_report.model_dump_json(indent=2), encoding="utf-8"
                    )

            if not self.config.skip_calibration:
                from skill_eval.calibration.calibrator import CalibrationChecker
                calibrator = CalibrationChecker()
                calibration_report = calibrator.calibrate(overall_grading, compliance=compliance_report)
                (config_dir / "calibration.json").write_text(
                    calibration_report.model_dump_json(indent=2), encoding="utf-8"
                )

            run_debug = {
                "config": config_name,
                "eval_name": eval_case.get_name(),
                "eval_id": eval_case.id,
                "run_num": run_num,
                "prompt": prompt[:500],
                "duration_ms": run_output.duration_ms,
                "total_tokens": run_output.total_tokens,
                "num_turns": run_output.num_turns,
                "stop_reason": run_output.stop_reason,
                "tool_calls": run_output.tool_calls,
                "tool_trace": [t.model_dump() for t in run_output.tool_trace],
                "result_text": run_output.result_text[:2000] if run_output.result_text else "",
                "transcript_preview": run_output.transcript[:2000] if run_output.transcript else "",
                "grading_summary": overall_grading.summary.model_dump(),
            }
            (config_dir / "run_debug.json").write_text(
                json.dumps(run_debug, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            run_duration = time.time() - run_start

            # Check for UNEVAL results that need retry
            uneval_count = sum(
                1 for e in all_expectations
                if getattr(e, "verdict", None) == AssertionVerdict.UNEVAL
            )
            if uneval_count > 0 and retry_num < self.config.max_retries_per_eval:
                self._emit_progress(
                    eval_case.get_name(), config_name,
                    eval_case.id, len(evals),
                    completed[0], total_jobs,
                    overall_grading, run_duration, run_output,
                    phase="retrying", substep=f"{uneval_count} UNEVAL assertions, retry {retry_num+1}/{self.config.max_retries_per_eval}",
                )
                return run_single_eval(eval_case, config_name, run_num, retry_num + 1)

            with lock:
                completed[0] += 1

            self._emit_progress(
                eval_case.get_name(), config_name,
                eval_case.id, len(evals),
                completed[0], total_jobs,
                overall_grading, run_duration, run_output,
            )
            return run_label

        jobs = []
        for eval_case in evals:
            for config_name in ("with_skill", "without_skill"):
                for run_num in range(1, self.config.runs_per_config + 1):
                    jobs.append((eval_case, config_name, run_num))

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {pool.submit(run_single_eval, *job): job for job in jobs}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    job = futures[future]
                    with lock:
                        completed[0] += 1

        return iteration_dir

    def _run_merged(self, skill_path: Path, eval_set: EvalSet, evals: list[EvalCase]) -> Path:
        output_dir = self.config.output_dir
        iteration_dir = output_dir / "iteration-1"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        merged_prompt = self._build_merged_prompt_from_evals(evals)
        num_checkpoints = len(evals)

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

                for eval_case in evals:
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
                        cwd=outputs_dir,
                    )
                except ClaudeRunnerError as exc:
                    run_duration = time.time() - run_start
                    (run_dir / "error.txt").write_text(str(exc), encoding="utf-8")
                    grading = GradingResult(
                        summary=GradingSummary(pass_rate=0.0, passed=0, failed=0, total=0),
                        expectations=[],
                        error=str(exc),
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
                    evals, run_output.transcript, outputs_dir,
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

                if not self.config.skip_calibration:
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
                else:
                    calibration_report = None

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
                }
                if calibration_report:
                    run_debug["calibration_grade"] = calibration_report.grade.value
                    run_debug["calibration_confidence"] = calibration_report.overall_confidence
                    run_debug["calibration_agreement_rate"] = calibration_report.agreement_rate
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
    def _build_merged_prompt_from_evals(evals: list[EvalCase]) -> str:
        sections = []
        for eval_case in evals:
            sections.append(f"## {eval_case.get_name()}\n{eval_case.prompt}")
        return (
            f"Please complete ALL of the following tasks in sequence.\n"
            f"Produce all outputs in a single response without asking for clarification.\n\n"
            + "\n\n".join(sections)
        )

    def _score_checkpoints(
        self,
        eval_cases: list[EvalCase],
        transcript: str,
        outputs_dir: Path,
        tool_trace: list | None = None,
        num_turns: int = 0,
        stop_reason: str = "",
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
                cp_prog = prog_checker.check(
                    eval_case, outputs_dir,
                    transcript=transcript,
                    tool_trace=tool_trace,
                    num_turns=num_turns,
                    stop_reason=stop_reason,
                )
                all_expectations.extend(cp_prog)
                prog_results_by_cp[cp_idx] = cp_prog

            checkpoint_list += f"\n### Checkpoint {cp_idx}: {eval_case.get_name()}\n"
            for i, exp in enumerate(eval_case.expectations):
                checkpoint_list += f"  [{len(expectation_map) - len(eval_case.expectations) + i}] {exp}\n"

        if not expectation_map:
            return all_expectations, per_checkpoint

        # Also grade Tier 3 assertions via LLM
        from skill_eval.scoring.llm_grader import LLMGrader
        grader = LLMGrader(
            model=self.scoring_config.llm_model,
            timeout=self.scoring_config.llm_timeout,
        )
        tier3_results_by_cp: dict[int, list] = {}
        if self.scoring_config.enable_llm:
            for cp_idx, eval_case in enumerate(eval_cases):
                tier3 = [a for a in eval_case.assertions if getattr(a, "tier", None) == "TIER3"]
                if tier3:
                    tier3_results = grader.grade_tier3(tier3, transcript, outputs_dir)
                    tier3_results_by_cp[cp_idx] = tier3_results
                    all_expectations.extend(tier3_results)

        prompt = (
            f"You are a checkpoint evaluator. Determine which expectations are covered in the execution transcript.\n\n"
            f"## Expectations ({len(expectation_map)} total across {len(eval_cases)} checkpoints)\n"
            f"{checkpoint_list}\n\n"
            f"## Transcript\n{transcript[:12000]}\n\n"
            f"## Output Format\n\n"
            f"Respond with ONLY a JSON array:\n"
            f'[{{"index": 0, "passed": true, "evidence": "brief reason"}}]\n\n'
            f"Judge each independently. passed=true only if there is clear evidence in the transcript.\n"
            f"If unable to judge, use `passed: false` with evidence starting with 'Not evaluated:' and explain why.\n"
            f"All evidence strings must be in Chinese (中文)."
        )

        response = grader._call_claude(prompt)

        results_by_index: dict[int, tuple[bool, str]] = {}
        array_match = re.search(r"\[[\s\S]*\]", response)
        if array_match:
            try:
                items = json.loads(array_match.group())
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
            passed, evidence = results_by_index.get(global_idx, (False, "Not evaluated: no grading response"))
            verdict = AssertionVerdict.PASS if passed else (
                AssertionVerdict.UNEVAL if evidence.startswith("Not evaluated") else AssertionVerdict.FAIL
            )
            exp_result = ExpectationResult(
                text=exp_text,
                passed=passed,
                evidence=evidence,
                source="llm_batch",
                verdict=verdict,
            )
            all_expectations.append(exp_result)
            exp_results_by_cp.setdefault(cp_idx, []).append(exp_result)

        for cp_idx, eval_case in enumerate(eval_cases):
            cp_exps = exp_results_by_cp.get(cp_idx, [])
            cp_assertions = prog_results_by_cp.get(cp_idx, [])
            cp_tier3 = tier3_results_by_cp.get(cp_idx, [])
            cp_all = cp_assertions + cp_tier3 + cp_exps
            cp_passed = sum(1 for e in cp_all if e.passed)
            cp_total = len(cp_all)
            per_checkpoint.append({
                "eval_id": eval_case.id,
                "eval_name": eval_case.get_name(),
                "passed": cp_passed,
                "failed": cp_total - cp_passed,
                "total": cp_total,
                "uneval": sum(1 for e in cp_all if getattr(e, "verdict", None) == AssertionVerdict.UNEVAL),
                "pass_rate": cp_passed / cp_total if cp_total > 0 else 0.0,
                "assertions": [
                    {
                        "text": e.text,
                        "passed": e.passed,
                        "evidence": e.evidence,
                        "source": e.source,
                        "verdict": getattr(e, "verdict", None).value if getattr(e, "verdict", None) else None,
                    }
                    for e in cp_assertions
                ],
                "expectations": [
                    {
                        "text": e.text,
                        "passed": e.passed,
                        "evidence": e.evidence,
                        "source": e.source,
                        "verdict": getattr(e, "verdict", None).value if getattr(e, "verdict", None) else None,
                    }
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

    @staticmethod
    def detect_outliers(durations: list[dict], threshold_multiplier: float = 3.0) -> list[dict]:
        return detect_outliers(durations, threshold_multiplier)

    @staticmethod
    def detect_timeout(durations: list[dict], timeout_seconds: int) -> list[dict]:
        return detect_timeout(durations, timeout_seconds)
