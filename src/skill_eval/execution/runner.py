"""Execution engine — ABTestRunner (orchestrates A/B evaluations).

ClaudeRunner lives in cli_runner.py. Anomaly detection lives in anomaly.py.
"""

from __future__ import annotations

import json
import re
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

ProgressCallback = Callable[..., None]


def _copy_skill_references(skill_path: Path | None, work_dir: Path) -> None:
    """把 skill 的 references/ 拷进考场 work_dir,使 body 中的 references/xxx 引用可读。

    skill body 常引用 references/xxx 作为领域知识;若不拷进 Claude 的 cwd,它会反复
    尝试读取不存在的文件直至 timeout。references/ 不存在则无操作;目标已存在则不覆盖。
    """
    if not skill_path:
        return
    src = skill_path / "references"
    if not src.is_dir():
        return
    dst = work_dir / "references"
    if dst.exists():
        return
    shutil.copytree(src, dst)


def build_sides(
    skill_path: Path,
    skill_b: Path | None,
    label_a: str | None,
    label_b: str | None,
) -> list[tuple[str, Path | None, str]]:
    """构建 sides spec:[(config_name, inject_skill, comp_label), ...]。

    - skill_b=None → 默认 with/without(side B 注入 None = skill 关 / baseline)
    - skill_b 给定 → v1/v2(两边都注入各自 skill,都开)
    v1/v2 的 config_name/label 默认取 skill 目录 basename,可被 label_a/b 覆盖。
    side A 恒为 positional skill(skill_path)。"""
    if skill_b is None:
        return [("with_skill", skill_path, "with"), ("without_skill", None, "without")]
    la = label_a or skill_path.name
    lb = label_b or skill_b.name
    return [(la, skill_path, la), (lb, skill_b, lb)]


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

    def run_ab_test(self, skill_path: Path, eval_set: EvalSet, sides: list | None = None) -> Path:
        if sides is None:
            sides = build_sides(skill_path, None, None, None)
        evals_to_run = self._sample_evals(eval_set.evals)
        return self._run_per_eval(skill_path, eval_set, evals_to_run, sides)

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

    def _run_per_eval(self, skill_path: Path, eval_set: EvalSet, evals: list[EvalCase], sides: list) -> Path:
        output_dir = self.config.output_dir
        iteration_dir = output_dir / "iteration-1"
        iteration_dir.mkdir(parents=True, exist_ok=True)

        total_jobs = len(evals) * len(sides) * self.config.runs_per_config
        completed = [0]
        lock = threading.Lock()

        max_retries = self.config.max_retries_per_eval

        def run_single_eval(eval_case: EvalCase, config_name: str, inject_skill: Path | None, run_num: int, retry_num: int = 0) -> str:
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
            # setup_source 钉主 skill(positional skill_path)对两边都用 —— 同卷保证:
            # v1/v2 只注入的 skill body 不同,源材料恒同(各用各的 skill dir 会拿不同 fixture)。
            source_path, source_name = eval_case.setup_source(skill_path, outputs_dir)
            # references/ 是该 side 注入的 skill body 常引用的领域知识;有注入才拷(默认 without 注入 None→不拷)。
            if inject_skill:
                _copy_skill_references(inject_skill, outputs_dir)

            run_start = time.time()
            use_skill = inject_skill
            prompt = eval_case.prompt
            if source_path:
                prompt += f"\n\nSOURCE PROJECT AVAILABLE: '{source_name}' copied to working directory at {source_path}"
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
                    timeout=eval_case.timeout or self.config.timeout_seconds,
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
                    return run_single_eval(eval_case, config_name, inject_skill, run_num, retry_num + 1)
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

            try:
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

                overall_grading = GradingResult(
                    expectations=all_expectations,
                    summary=GradingSummary.from_expectations(all_expectations),
                )
                (config_dir / "grading.json").write_text(overall_grading.model_dump_json(indent=2), encoding="utf-8")
                (config_dir / "checkpoints.json").write_text(
                    json.dumps(per_checkpoint, indent=2, ensure_ascii=False), encoding="utf-8"
                )

                compliance_report = None
                # compliance 按"该 side 注入的 skill"判:凡 inject_skill 非空且未 skip 就跑
                # (默认 with→跑、without→不跑;v1/v2 两边都跑)。extract body 取自 inject_skill。
                if inject_skill and not self.config.skip_compliance:
                    self._emit_progress(
                        eval_case.get_name(), config_name,
                        eval_case.id, len(evals),
                        completed[0], total_jobs,
                        None, 0.0, None, phase="compliance",
                    )
                    from skill_eval.compliance.extractor import InstructionExtractor
                    from skill_eval.compliance.checker import ComplianceChecker

                    extractor = InstructionExtractor()
                    _, _, _, _, skill_body = parse_skill_md(inject_skill)
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
                # dump raw stream-json 成 transcript.jsonl,打通 live 真跑→audit 闭环:
                # _parse_stream_json 只留 assistant text + 截断的 tool_trace,丢了 tool_use
                # 完整 input 与 tool_result 配对;而 audit 的 build_digest 需要完整配对重建
                # digest。stream-json 的 assistant/user event 本身带 message 字段 → build_digest
                # 可直接解析(result event 无 message,自动跳过)。使 run(路径1/3)产物能喂
                # audit(路径2)/ init --from-transcript(路径3)。
                if run_output.stdout:
                    (config_dir / "transcript.jsonl").write_text(
                        run_output.stdout, encoding="utf-8"
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
                    return run_single_eval(eval_case, config_name, inject_skill, run_num, retry_num + 1)

                with lock:
                    completed[0] += 1

                self._emit_progress(
                    eval_case.get_name(), config_name,
                    eval_case.id, len(evals),
                    completed[0], total_jobs,
                    overall_grading, run_duration, run_output,
                )
                return run_label

            except Exception as exc:
                import traceback
                run_duration = time.time() - run_start
                (config_dir / "error.txt").write_text(f"{exc}\n{traceback.format_exc()}", encoding="utf-8")
                grading = GradingResult(
                    summary=GradingSummary(pass_rate=0.0, passed=0, failed=0, total=0),
                    expectations=[ExpectationResult(text=e, passed=False, evidence=str(exc), source="error")
                                  for e in eval_case.expectations],
                    error=f"{type(exc).__name__}: {exc}",
                )
                (config_dir / "grading.json").write_text(grading.model_dump_json(indent=2), encoding="utf-8")
                traceback.print_exc()
                with lock:
                    completed[0] += 1
                self._emit_progress(
                    eval_case.get_name(), config_name,
                    eval_case.id, len(evals),
                    completed[0], total_jobs,
                    grading, run_duration, None,
                    phase="failed",
                    substep=f"{type(exc).__name__}",
                )
                return run_label

        jobs = []
        for eval_case in evals:
            for config_name, inject_skill, _comp_label in sides:
                for run_num in range(1, self.config.runs_per_config + 1):
                    jobs.append((eval_case, config_name, inject_skill, run_num))

        with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
            futures = {pool.submit(run_single_eval, *job): job for job in jobs}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    job = futures[future]
                    import traceback
                    traceback.print_exc()
                    print(f"[skill-eval] Unhandled error in run {job}: {exc}", file=__import__("sys").stderr, flush=True)
                    with lock:
                        completed[0] += 1

        return iteration_dir

    def _score_checkpoints(
        self,
        eval_cases: list["EvalCase"],
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

        checkpoint_results: list[dict] = []
        all_expectations: list = []

        for eval_case in eval_cases:
            cp: dict = {
                "eval_id": eval_case.id,
                "eval_name": eval_case.get_name(),
                "passed": 0,
                "failed": 0,
                "total": 0,
                "pass_rate": 0.0,
                "expectations": [],
            }

            prog_results = []
            if prog_checker:
                prog_results = prog_checker.check(eval_case, outputs_dir, transcript=transcript, tool_trace=tool_trace, num_turns=num_turns, stop_reason=stop_reason)

            llm_results = []
            tier3_results = []
            if self.scoring_config.enable_llm:
                from skill_eval.scoring.llm_grader import LLMGrader
                grader = LLMGrader(model=self.scoring_config.llm_model, timeout=self.scoring_config.llm_timeout)

                if eval_case.expectations:
                    llm_grading = grader.grade(
                        eval_case=eval_case,
                        transcript=transcript,
                        output_dir=outputs_dir,
                    )
                    llm_results = llm_grading.expectations if llm_grading else []

                tier3_assertions = [a for a in eval_case.assertions if a.tier and a.tier.value == "TIER3"]
                if tier3_assertions:
                    tier3_results = grader.grade_tier3(tier3_assertions, transcript, outputs_dir)

            merged = prog_results[:]
            merged.extend(llm_results)
            merged.extend(tier3_results)

            cp["expectations"] = [e.model_dump(mode="json") for e in merged]
            cp["total"] = len(merged)
            cp["passed"] = sum(1 for e in merged if e.passed)
            cp["failed"] = cp["total"] - cp["passed"]
            cp["pass_rate"] = cp["passed"] / cp["total"] if cp["total"] > 0 else 0.0

            checkpoint_results.append(cp)
            all_expectations.extend(merged)

        return all_expectations, checkpoint_results

    def _emit_progress(
        self,
        eval_name: str = "",
        config_name: str = "",
        eval_idx: int = 0,
        total_evals: int = 0,
        completed: int = 0,
        total: int = 0,
        grading=None,
        run_duration: float = 0.0,
        run_output=None,
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
