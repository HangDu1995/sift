"""tests/test_changed_from.py — run --changed-from:改 skill 后自动判受影响 eval(路 A)。"""
from __future__ import annotations

import pytest

from skill_eval.cli.affected import _parse_ids
from skill_eval.errors import SkillEvalError

from pathlib import Path

from skill_eval.cli import affected
from skill_eval.models import EvalCase, EvalSet


class TestParseIds:
    def test_valid_json(self):
        assert _parse_ids('{"affected_ids": [3, 1, 5]}', valid={1, 2, 3, 5}) == [3, 1, 5]

    def test_dedup_preserve_order(self):
        assert _parse_ids('{"affected_ids": [3, 3, 1]}', valid={1, 3}) == [3, 1]

    def test_empty_response_raises(self):
        with pytest.raises(SkillEvalError, match="返回空"):
            _parse_ids("", valid={1})

    def test_whitespace_response_raises(self):
        with pytest.raises(SkillEvalError, match="返回空"):
            _parse_ids("   \n  ", valid={1})

    def test_malformed_json_raises(self):
        with pytest.raises(SkillEvalError, match="解析不出"):
            _parse_ids("not json at all", valid={1})

    def test_no_list_raises(self):
        with pytest.raises(SkillEvalError, match="affected_ids 列表"):
            _parse_ids('{"affected_ids": "oops"}', valid={1})

    def test_non_int_raises(self):
        with pytest.raises(SkillEvalError, match="非整数"):
            _parse_ids('{"affected_ids": [1, "x"]}', valid={1})

    def test_id_not_in_valid_raises_with_available(self):
        with pytest.raises(SkillEvalError) as exc:
            _parse_ids('{"affected_ids": [99]}', valid={1, 2})
        msg = str(exc.value)
        assert "99" in msg and "1" in msg and "2" in msg  # 列出可用


def _write_skill(skill_dir: Path, body: str, name: str = "t") -> Path:
    """写一个最小合法 SKILL.md(frontmatter + body)到 skill_dir。"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return skill_dir


def _eval_set(*ids: int) -> EvalSet:
    return EvalSet(skill_name="t", evals=[EvalCase(id=i, prompt=f"task {i}", expectations=[f"ok {i}"]) for i in ids])


class TestComputeAffected:
    def test_success_returns_ids(self, tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "## 步骤\n1. 校验 id 为正整数")
        base = _write_skill(tmp_path / "base", "## 步骤\n1. 校验 id")
        monkeypatch.setattr(
            affected, "call_claude", lambda *a, **k: '{"affected_ids": [1, 3]}'
        )
        ids = affected.compute_affected_evals(cur, base, _eval_set(1, 2, 3), model=None)
        assert ids == [1, 3]

    def test_call_failure_raises(self, tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        base = _write_skill(tmp_path / "base", "body")
        def boom(*a, **k):
            raise RuntimeError("api 529")
        monkeypatch.setattr(affected, "call_claude", boom)
        with pytest.raises(SkillEvalError, match="LLM 调用出错"):
            affected.compute_affected_evals(cur, base, _eval_set(1), model=None)

    def test_empty_result_returns_empty(self, tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        base = _write_skill(tmp_path / "base", "body")
        monkeypatch.setattr(affected, "call_claude", lambda *a, **k: '{"affected_ids": []}')
        assert affected.compute_affected_evals(cur, base, _eval_set(1, 2), model=None) == []

    def test_missing_baseline_raises(self, tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        # baseline 目录无 SKILL.md
        with pytest.raises(SkillEvalError, match="读不出 SKILL.md"):
            affected.compute_affected_evals(cur, tmp_path / "nobase", _eval_set(1), model=None)


class TestBenchmarkSelection:
    def test_selection_field_optional(self):
        from skill_eval.models.batch_models import BenchmarkData, BenchmarkMetadata
        b = BenchmarkData(metadata=BenchmarkMetadata(skill_name="t"))  # 默认无 selection
        assert b.selection is None

    def test_selection_field_roundtrip(self):
        from skill_eval.models.batch_models import BenchmarkData, BenchmarkMetadata
        sel = {"mode": "changed-from", "ids": [3, 5], "affected": 2, "total": 12}
        b = BenchmarkData(metadata=BenchmarkMetadata(skill_name="t"), selection=sel)
        assert b.selection == sel
        assert b.model_dump(mode="json")["selection"] == sel


class TestPipelineChangedFrom:
    def _eval_set(self):
        return EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x"), EvalCase(id=2, prompt="y")])

    def test_mutex_with_eval_ids(self, tmp_path):
        from skill_eval.cli.pipeline import _run_ab_eval
        with pytest.raises(SkillEvalError):
            _run_ab_eval(
                skill_path=tmp_path / "skill", eval_set=self._eval_set(), runs=1,
                model=None, workers=1, timeout=10, max_turns=1, scoring="hybrid",
                no_calibration=True, eval_ids=[1], changed_from=tmp_path / "base",
            )

    def test_mutex_with_eval_samples(self, tmp_path):
        from skill_eval.cli.pipeline import _run_ab_eval
        with pytest.raises(SkillEvalError):
            _run_ab_eval(
                skill_path=tmp_path / "skill", eval_set=self._eval_set(), runs=1,
                model=None, workers=1, timeout=10, max_turns=1, scoring="hybrid",
                no_calibration=True, eval_samples=2, changed_from=tmp_path / "base",
            )

    def test_empty_result_exits_before_run(self, tmp_path, monkeypatch):
        # 判定空集 → 在跑 cc 前就 typer.Exit(0)(合法,不是失败)。
        # patch 源模块:pipeline 内是 lazy `from skill_eval.cli.affected import
        # compute_affected_evals`,运行时从 affected 取,故须 patch affected 命名空间。
        import typer
        from skill_eval.cli.pipeline import _run_ab_eval
        monkeypatch.setattr(
            "skill_eval.cli.affected.compute_affected_evals", lambda *a, **k: []
        )
        with pytest.raises(typer.Exit) as exc:
            _run_ab_eval(
                skill_path=tmp_path / "skill", eval_set=self._eval_set(), runs=1,
                model=None, workers=1, timeout=10, max_turns=1, scoring="hybrid",
                no_calibration=True, changed_from=tmp_path / "base",
            )
        assert exc.value.exit_code == 0
