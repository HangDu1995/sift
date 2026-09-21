"""run 进度渲染(_ProgressState):顶部 live bar + 每次执行完成追加持久日志行。

测渲染逻辑本身(直接喂数据,不 spawn cc)。非 tty 路径只流式打持久行(断言简单);
tty 路径另用 _FakeTTY 验 live 块 + ANSI 擦写 + 持久行共存。
"""

from __future__ import annotations

import io
import sys

from sift.cli.progress import ProgressUpdate, _config_label, _ProgressState
from sift.models.grading_models import (
    ExpectationResult,
    GradingResult,
    GradingSummary,
)


def _grading(exps):
    n = len(exps)
    p = sum(1 for e in exps if e.passed)
    return GradingResult(
        summary=GradingSummary(passed=p, failed=n - p, total=n, pass_rate=(p / n if n else 0.0)),
        expectations=exps,
    )


class _FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_config_label():
    assert _config_label("with_skill") == "with"
    assert _config_label("without_skill") == "BASE"
    assert _config_label("data-transform") == "data-transform"
    assert _config_label("agent") == "agent"  # agent 评测 config_name(T9 锁定 fallback 兜住)


def test_total_runs_reflects_num_sides():
    """agent 评测(num_sides=1)→ total_runs = num_sides*runs*evals(非硬编码 2×);默认 2 侧不变。"""
    assert _ProgressState(num_evals=2, runs_per_config=1, num_sides=1).total_runs == 2
    assert _ProgressState(num_evals=2, runs_per_config=1).total_runs == 4  # 默认 2 臂回归


def test_completion_emits_persistent_line(monkeypatch):
    """一次执行完成 → 流式打一行:config/题名/通过率/耗时 + 失败项(标客观)。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=2, runs_per_config=1)  # total_runs=4
    exps = [
        ExpectationResult(text="ok1", passed=True, evidence=""),
        ExpectationResult(text="ok2", passed=True, evidence=""),
        ExpectationResult(text="ok3", passed=True, evidence=""),
        ExpectationResult(text="total_errors 为 0", passed=False, evidence="", source="programmatic"),
    ]
    st.update(ProgressUpdate(**_progress_update_values(grading=_grading(exps), run_duration=113.0)))
    out = err.getvalue()
    assert "✓ 1/4" in out
    assert "标准日志分析流程" in out
    assert "with" in out
    assert "75% (3/4)" in out
    assert "[客观] total_errors 为 0" in out


def test_inflight_emits_nothing(monkeypatch):
    """在跑事件(executing/scoring,completed 不自增)→ 非 tty 下不打任何行。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=2, runs_per_config=1)
    st.update(ProgressUpdate(**_progress_update_values_phase()))
    st.update(ProgressUpdate(**_progress_update_values_phase(phase="scoring")))
    assert err.getvalue() == "", "在跑事件不该打持久行"


def test_retry_not_logged(monkeypatch):
    """retrying 带 grading 但 completed 未自增 → 不打持久行(重试不是一次完成)。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=2, runs_per_config=1)
    g = _grading([ExpectationResult(text="e", passed=False, evidence="", source="error")])
    st.update(ProgressUpdate(**_progress_update_values_substep(grading=g)))
    assert err.getvalue() == "", "retry 不该打持久行"


def test_failed_phase_line(monkeypatch):
    """执行失败(phase=failed,completed 自增)→ 打「执行失败」行,不列 expectation。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=2, runs_per_config=1)
    g = _grading([ExpectationResult(text="e", passed=False, evidence="", source="error")])
    st.update(
        ProgressUpdate(
            **_progress_update_values_substep(
                config_name="without_skill",
                completed=1,
                grading=g,
                run_duration=8.0,
                phase="failed",
                substep="ClaudeRunnerError",
            )
        )
    )
    out = err.getvalue()
    assert "✗ 1/4" in out
    assert "执行失败" in out
    assert "BASE" in out
    assert "[客观]" not in out and "[主观]" not in out  # failed 不列 expectation


def test_failed_items_tagged_objective_vs_subjective(monkeypatch):
    """失败项按 source 标:programmatic→客观,llm→主观。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=1, runs_per_config=1)  # total=2
    exps = [
        ExpectationResult(text="ok", passed=True, evidence=""),
        ExpectationResult(text="客观失败", passed=False, evidence="", source="programmatic"),
        ExpectationResult(text="主观失败", passed=False, evidence="", source="llm"),
    ]
    st.update(ProgressUpdate(**_progress_update_values(eval_name="e", total_evals=1, total=2, grading=_grading(exps))))
    out = err.getvalue()
    assert "[客观] 客观失败" in out
    assert "[主观] 主观失败" in out


def test_completions_accumulate_per_execution(monkeypatch):
    """多次完成各打一行、按 completed 编号 1/4, 2/4…;config 各自标注(with/BASE)。"""
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=2, runs_per_config=1)  # total=4
    g_ok = _grading([ExpectationResult(text="ok", passed=True, evidence="")])
    st.update(ProgressUpdate(**_progress_update_values(eval_name="题A", grading=g_ok)))
    st.update(
        ProgressUpdate(
            **_progress_update_values(
                eval_name="题A", config_name="without_skill", completed=2, grading=g_ok, run_duration=11.0
            )
        )
    )
    st.update(
        ProgressUpdate(
            **_progress_update_values(eval_name="题B", eval_idx=2, completed=3, grading=g_ok, run_duration=12.0)
        )
    )
    out = err.getvalue()
    assert "✓ 1/4" in out and "✓ 2/4" in out and "✓ 3/4" in out
    line1 = [l for l in out.splitlines() if "1/4" in l][0]
    line2 = [l for l in out.splitlines() if "2/4" in l][0]
    assert "with" in line1 and "BASE" in line2


def test_tty_renders_live_block_and_persistent(monkeypatch):
    """tty:executing 画 live 块(bar+⏳),完成时擦旧 live 块、打持久行、重画 live。"""
    err = _FakeTTY()
    monkeypatch.setattr(sys, "stderr", err)
    st = _ProgressState(num_evals=1, runs_per_config=1)  # total=2

    st.update(ProgressUpdate(**_progress_update_values_phase(total_evals=1, total=2)))
    after_exec = err.getvalue()
    assert "\033[" not in after_exec  # 首次无旧块可擦
    assert "░" in after_exec  # 空 bar(0%)
    assert "⏳" in after_exec and "标准日志分析流程" in after_exec

    g = _grading([ExpectationResult(text="ok", passed=True, evidence="")])
    st.update(ProgressUpdate(**_progress_update_values(total_evals=1, total=2, grading=g, run_duration=12.0)))
    out = err.getvalue()
    assert "\033[" in out  # 擦了旧 live 块
    assert "✓ 1/2" in out  # 持久行
    assert "50% (1/2)" in out  # live bar 更新到一半


def _progress_update_values(**changes):
    data = {
        "eval_name": "标准日志分析流程",
        "config_name": "with_skill",
        "eval_idx": 1,
        "total_evals": 2,
        "completed": 1,
        "total": 4,
        "grading": None,
        "run_duration": 10.0,
        "run_output": None,
    }
    data.update(changes)
    return data


def _progress_update_values_phase(**changes):
    data = {
        "eval_name": "标准日志分析流程",
        "config_name": "with_skill",
        "eval_idx": 1,
        "total_evals": 2,
        "completed": 0,
        "total": 4,
        "grading": None,
        "run_duration": 0.0,
        "run_output": None,
        "phase": "executing",
    }
    data.update(changes)
    return data


def _progress_update_values_substep(**changes):
    data = {
        "eval_name": "x",
        "config_name": "with_skill",
        "eval_idx": 1,
        "total_evals": 2,
        "completed": 0,
        "total": 4,
        "grading": None,
        "run_duration": 5.0,
        "run_output": None,
        "phase": "retrying",
        "substep": "error, retry 1/2",
    }
    data.update(changes)
    return data
