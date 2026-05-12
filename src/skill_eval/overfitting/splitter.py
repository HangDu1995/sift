"""Eval splitter — splits eval cases into train/holdout sets."""

from __future__ import annotations

import json
import random
from pathlib import Path

from skill_eval.models import EvalSet


class EvalSplitter:
    def split(
        self,
        eval_set: EvalSet,
        holdout_fraction: float = 0.3,
        seed: int = 42,
    ) -> tuple[EvalSet, EvalSet]:
        if holdout_fraction <= 0 or holdout_fraction >= 1:
            raise ValueError("holdout_fraction must be between 0 and 1")

        evals = list(eval_set.evals)
        if len(evals) < 2:
            raise ValueError("Need at least 2 eval cases to split")

        levels = {}
        for e in evals:
            levels.setdefault(e.level, []).append(e)

        train_evals = []
        holdout_evals = []
        rng = random.Random(seed)

        for level_cases in levels.values():
            n_holdout = max(1, int(len(level_cases) * holdout_fraction))
            shuffled = sorted(level_cases, key=lambda _: rng.random())
            holdout_evals.extend(shuffled[:n_holdout])
            train_evals.extend(shuffled[n_holdout:])

        train_set = EvalSet(skill_name=eval_set.skill_name, evals=train_evals)
        holdout_set = EvalSet(skill_name=eval_set.skill_name, evals=holdout_evals)

        return train_set, holdout_set

    @staticmethod
    def save(eval_set: EvalSet, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(eval_set.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def load(path: Path) -> EvalSet:
        return EvalSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
