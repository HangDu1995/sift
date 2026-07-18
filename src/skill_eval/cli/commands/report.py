from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console


def generate_report_html(workspace: Path) -> Path | None:
    """从 workspace 的 benchmark.json(+ scorecard.json)生成自包含 report.html。best-effort。

    生产者(run 末尾自动产)与 `report` 命令共用此「workspace → report.html」核心;report 命令
    额外支持 --benchmark/--previous 覆盖与候选路径搜索(workspace vs iteration-1)。无
    benchmark.json → 返回 None(没东西可报,如路径1 --task 模式无 benchmark)。ReportGenerator
    .write_static 内部自读 vet-report.json / gate.json(存在则带对应面板/徽章)。
    """
    bench_path = workspace / "benchmark.json"
    if not bench_path.exists():
        return None
    bench_data = json.loads(bench_path.read_text(encoding="utf-8"))

    scorecard_data = None
    sc_path = workspace / "scorecard.json"
    if sc_path.exists():
        try:
            scorecard_data = json.loads(sc_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    from skill_eval.reporting.viewer import ReportGenerator
    output_path = workspace / "report.html"
    ReportGenerator().write_static(workspace, output_path, bench_data, previous=None, scorecard=scorecard_data)
    return output_path


@app.command()
def report(
    workspace: Path = typer.Argument(..., help="Path to evaluation workspace"),
    benchmark: Optional[Path] = typer.Option(None, "--benchmark", help="Path to benchmark.json"),
    previous: Optional[Path] = typer.Option(None, "--previous", help="Path to previous benchmark.json"),
    scorecard: Optional[Path] = typer.Option(None, "--scorecard", "--static", help="Path to scorecard.json"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser after generation"),
):
    """Generate a static HTML report from evaluation results."""
    from skill_eval.reporting.viewer import ReportGenerator

    bench_data = None
    bench_path = benchmark
    if not bench_path or not bench_path.exists():
        for candidate in [workspace / "benchmark.json", workspace / "iteration-1" / "benchmark.json"]:
            if candidate.exists():
                bench_path = candidate
                break
    if bench_path and bench_path.exists():
        bench_data = json.loads(bench_path.read_text(encoding="utf-8"))

    scorecard_path = scorecard
    if not scorecard_path or not scorecard_path.exists():
        for candidate in [workspace / "scorecard.json", workspace / "iteration-1" / "scorecard.json"]:
            if candidate.exists():
                scorecard_path = candidate
                break
    scorecard_data = None
    if scorecard_path and scorecard_path.exists():
        try:
            scorecard_data = json.loads(scorecard_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    prev_data = None
    if previous and previous.exists():
        from skill_eval.reporting.viewer import load_previous_iteration

        prev_data = load_previous_iteration(previous) if previous.is_dir() else None
        if prev_data is None:
            try:
                prev_runs_data = json.loads(previous.read_text(encoding="utf-8"))
                prev_data = {"runs": prev_runs_data.get("runs", [])}
            except Exception:
                pass

    gen = ReportGenerator()
    output_path = workspace / "report.html"
    gen.write_static(workspace, output_path, bench_data, previous=prev_data, scorecard=scorecard_data)
    console.print(f"Report written to {output_path}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{output_path.resolve()}")


@app.command(name="batch-report")
def batch_report(
    workspace: Path = typer.Argument(..., help="Path to batch workspace directory"),
    manifest: Optional[Path] = typer.Option(None, "--manifest", help="Path to manifest.json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output HTML file path"),
    open_browser: bool = typer.Option(False, "--open", help="Open report in browser"),
):
    """Generate HTML report from batch evaluation results."""
    from skill_eval.reporting.batch_viewer import generate_batch_html

    manifest_data = None
    if manifest and manifest.exists():
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            pass

    html = generate_batch_html(workspace, manifest_data)

    if not output:
        output = workspace / "batch-report.html"

    output.write_text(html, encoding="utf-8")
    console.print(f"Batch report written to {output}")

    if open_browser:
        import webbrowser
        webbrowser.open(f"file://{output.resolve()}")
