"""tests/test_changed_from.py — eval --changed-from:改 skill 后自动判受影响 eval(路 A)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sift.cli import affected
from sift.cli.affected import _parse_ids
from sift.cli.pipeline import EvaluationOptions, RunConfig
from sift.errors import SiftError
from sift.models import EvalCase, EvalSet


class TestParseIds:
    @staticmethod
    def test_valid_json():
        assert _parse_ids('{"affected_ids": [3, 1, 5]}', valid={1, 2, 3, 5}) == [3, 1, 5]

    @staticmethod
    def test_dedup_preserve_order():
        assert _parse_ids('{"affected_ids": [3, 3, 1]}', valid={1, 3}) == [3, 1]

    @staticmethod
    def test_empty_response_raises():
        with pytest.raises(SiftError, match="返回空"):
            _parse_ids("", valid={1})

    @staticmethod
    def test_whitespace_response_raises():
        with pytest.raises(SiftError, match="返回空"):
            _parse_ids("   \n  ", valid={1})

    @staticmethod
    def test_malformed_json_raises():
        with pytest.raises(SiftError, match="解析不出"):
            _parse_ids("not json at all", valid={1})

    @staticmethod
    def test_no_list_raises():
        with pytest.raises(SiftError, match="affected_ids 列表"):
            _parse_ids('{"affected_ids": "oops"}', valid={1})

    @staticmethod
    def test_non_int_raises():
        with pytest.raises(SiftError, match="非整数"):
            _parse_ids('{"affected_ids": [1, "x"]}', valid={1})

    @staticmethod
    def test_id_not_in_valid_raises_with_available():
        with pytest.raises(SiftError) as exc:
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
    @staticmethod
    def test_success_returns_ids(tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "## 步骤\n1. 校验 id 为正整数")
        base = _write_skill(tmp_path / "base", "## 步骤\n1. 校验 id")
        monkeypatch.setattr(affected, "call_claude", lambda *a, **k: '{"affected_ids": [1, 3]}')
        ids = affected.compute_affected_evals(cur, base, _eval_set(1, 2, 3), model=None)
        assert ids == [1, 3]

    @staticmethod
    def test_call_failure_raises(tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        base = _write_skill(tmp_path / "base", "body")

        def boom(*a, **k):
            raise RuntimeError("api 529")

        monkeypatch.setattr(affected, "call_claude", boom)
        with pytest.raises(SiftError, match="LLM 调用出错"):
            affected.compute_affected_evals(cur, base, _eval_set(1), model=None)

    @staticmethod
    def test_empty_result_returns_empty(tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        base = _write_skill(tmp_path / "base", "body")
        monkeypatch.setattr(affected, "call_claude", lambda *a, **k: '{"affected_ids": []}')
        assert affected.compute_affected_evals(cur, base, _eval_set(1, 2), model=None) == []

    @staticmethod
    def test_missing_baseline_raises(tmp_path, monkeypatch):
        cur = _write_skill(tmp_path / "cur", "body")
        # baseline 目录无 SKILL.md
        with pytest.raises(SiftError, match="读不出 SKILL.md"):
            affected.compute_affected_evals(cur, tmp_path / "nobase", _eval_set(1), model=None)


class TestBenchmarkSelection:
    @staticmethod
    def test_selection_field_optional():
        from sift.models.batch_models import BenchmarkData, BenchmarkMetadata

        b = BenchmarkData(metadata=BenchmarkMetadata(skill_name="t"))  # 默认无 selection
        assert b.selection is None

    @staticmethod
    def test_selection_field_roundtrip():
        from sift.models.batch_models import BenchmarkData, BenchmarkMetadata

        sel = {"mode": "changed-from", "ids": [3, 5], "affected": 2, "total": 12}
        b = BenchmarkData(metadata=BenchmarkMetadata(skill_name="t"), selection=sel)
        assert b.selection == sel
        assert b.model_dump(mode="json")["selection"] == sel


class TestPipelineChangedFrom:
    @staticmethod
    def _eval_set():
        return EvalSet(skill_name="t", evals=[EvalCase(id=1, prompt="x"), EvalCase(id=2, prompt="y")])

    def test_mutex_with_eval_ids(self, tmp_path):
        from sift.cli.pipeline import _run_ab_eval

        with pytest.raises(SiftError):
            _run_ab_eval(
                skill_path=tmp_path / "skill",
                eval_set=self._eval_set(),
                scoring="hybrid",
                run_config=_changed_run_config(eval_ids=[1]),
                options=EvaluationOptions(changed_from=tmp_path / "base"),
            )

    def test_mutex_with_eval_samples(self, tmp_path):
        from sift.cli.pipeline import _run_ab_eval

        with pytest.raises(SiftError):
            _run_ab_eval(
                skill_path=tmp_path / "skill",
                eval_set=self._eval_set(),
                scoring="hybrid",
                run_config=_changed_run_config(eval_samples=2),
                options=EvaluationOptions(changed_from=tmp_path / "base"),
            )

    def test_empty_result_exits_before_run(self, tmp_path, monkeypatch):
        # 判定空集 → 在跑 cc 前就 typer.Exit(0)(合法,不是失败)。
        # patch 源模块:pipeline 内是 lazy `from sift.cli.affected import
        # compute_affected_evals`,运行时从 affected 取,故须 patch affected 命名空间。
        import typer

        from sift.cli.pipeline import _run_ab_eval

        monkeypatch.setattr("sift.cli.affected.compute_affected_evals", lambda *a, **k: [])
        with pytest.raises(typer.Exit) as exc:
            _run_ab_eval(
                skill_path=tmp_path / "skill",
                eval_set=self._eval_set(),
                scoring="hybrid",
                run_config=_changed_run_config(),
                options=EvaluationOptions(changed_from=tmp_path / "base"),
            )
        assert exc.value.exit_code == 0


def _changed_run_config(**selection):
    return RunConfig(
        runs_per_config=1,
        model=None,
        max_workers=1,
        timeout_seconds=10,
        max_turns=1,
        skip_calibration=True,
        **selection,
    )
