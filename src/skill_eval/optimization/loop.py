"""Optimization loop — iteratively improves skill trigger descriptions."""

from __future__ import annotations

import random
from pathlib import Path

from skill_eval.models import (
    IterationRecord,
    LoopConfig,
    LoopResult,
    TriggerQuery,
)
from skill_eval.optimization.trigger_eval import TriggerEvaluator
from skill_eval.optimization.description_improver import DescriptionImprover
from skill_eval.parser import parse_skill_md


def split_eval_set(
    eval_set: list[TriggerQuery], holdout: float = 0.4, seed: int = 42,
) -> tuple[list[TriggerQuery], list[TriggerQuery]]:
    rng = random.Random(seed)
    trigger = [q for q in eval_set if q.should_trigger]
    no_trigger = [q for q in eval_set if not q.should_trigger]

    rng.shuffle(trigger)
    rng.shuffle(no_trigger)

    def split_list(lst: list[TriggerQuery]) -> tuple[list[TriggerQuery], list[TriggerQuery]]:
        n = max(1, int(len(lst) * holdout))
        return lst[:-n], lst[-n:]

    train_t, test_t = split_list(trigger)
    train_n, test_n = split_list(no_trigger)

    return train_t + train_n, test_t + test_n


class OptimizationLoop:
    def __init__(self, config: LoopConfig, project_root: Path | None = None):
        self.config = config
        self.project_root = project_root or Path.cwd()

    def run(self, skill_path: Path, eval_set: list[TriggerQuery]) -> LoopResult:
        name, original_description, content, _, _ = parse_skill_md(skill_path)

        train_set, test_set = split_eval_set(
            eval_set, self.config.holdout_fraction, self.config.seed
        )

        improver = DescriptionImprover(name, content, self.config.model)
        history: list[IterationRecord] = []
        best_description = original_description
        best_score = 0.0
        exit_reason = "max_iterations"

        current_desc = original_description

        for iteration in range(1, self.config.max_iterations + 1):
            evaluator = TriggerEvaluator(
                skill_name=name,
                skill_description=current_desc,
                num_workers=self.config.num_workers,
                timeout=self.config.timeout,
                project_root=self.project_root,
                runs_per_query=self.config.runs_per_query,
                trigger_threshold=self.config.trigger_threshold,
                model=self.config.model,
            )

            train_result = evaluator.evaluate(train_set)
            train_passed = train_result["summary"]["passed"]
            train_total = train_result["summary"]["total"]
            train_score = train_passed / train_total if train_total > 0 else 0.0

            test_passed = None
            test_total = None
            test_score = 0.0
            if test_set:
                test_result = evaluator.evaluate(test_set)
                test_passed = test_result["summary"]["passed"]
                test_total = test_result["summary"]["total"]
                test_score = test_passed / test_total if test_total > 0 else 0.0

            score = test_score if test_set else train_score
            if score > best_score:
                best_score = score
                best_description = current_desc

            record = IterationRecord(
                iteration=iteration,
                description=current_desc,
                train_passed=train_passed,
                train_total=train_total,
                test_passed=test_passed,
                test_total=test_total,
                results=train_result.get("results", []),
            )
            history.append(record)

            if train_passed == train_total:
                exit_reason = "all_train_passed"
                break

            eval_for_improver = train_result
            prev_descriptions = [{"description": r.description, "score": "unknown"} for r in history]
            current_desc = improver.improve(current_desc, eval_for_improver, prev_descriptions)

        return LoopResult(
            exit_reason=exit_reason,
            original_description=original_description,
            best_description=best_description,
            best_score=best_score,
            best_score_label="test" if test_set else "train",
            iterations_run=len(history),
            history=history,
        )
