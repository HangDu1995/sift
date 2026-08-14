"""format_failure_triage 纯函数测:从 runs 聚合失败根因为打印行。"""
from __future__ import annotations

from sift.cli.pipeline import format_failure_triage
from sift.models.benchmark_models import RunResultEntry, RunResultMetrics


def _entry(cause, suggestion):
    return RunResultEntry(
        eval_id=1, configuration="with_skill", run_number=1,
        result=RunResultMetrics(pass_rate=0.0, passed=0, failed=0, total=0),
        error_root_cause=cause, error_suggestion=suggestion,
    )


def test_no_errors_returns_empty():
    assert format_failure_triage([_entry(None, None)]) == []


def test_aggregates_by_cause_counts():
    runs = [
        _entry("timeout", "调大 --timeout"),
        _entry("timeout", "调大 --timeout"),
        _entry("rate_limited", "降并发"),
    ]
    lines = format_failure_triage(runs)
    joined = "\n".join(lines)
    assert "2× timeout" in joined
    assert "1× rate_limited" in joined
    assert "调大 --timeout" in joined
    assert "降并发" in joined


def test_header_present_when_errors_exist():
    lines = format_failure_triage([_entry("timeout", "x")])
    assert any("失败根因" in l for l in lines)
