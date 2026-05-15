from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.main import app, console


@app.command(name="vet-overfit")
def vet_overfit(
    domain_bench: Path = typer.Option(..., "--domain-bench", help="Path to domain benchmark.json"),
    general_bench: Optional[Path] = typer.Option(None, "--general-bench", help="Path to general benchmark.json"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Output path for overfitting report"),
    fmt: str = typer.Option("table", "--format", help="Output format: table or json"),
):
    """Detect overfitting by comparing domain vs general benchmarks."""
    from skill_eval.overfitting.gap import GapCalculator
    from skill_eval.overfitting.detector import OverfittingDetector

    gap_data = None

    if general_bench and general_bench.exists():
        try:
            domain_data = json.loads(domain_bench.read_text(encoding="utf-8"))
            general_data = json.loads(general_bench.read_text(encoding="utf-8"))
            calc = GapCalculator()
            gap = calc.calculate(domain_data, general_data)
            gap_data = gap.model_dump()
        except Exception as exc:
            console.print(f"[yellow]Warning:[/yellow] Could not calculate generalization gap: {exc}")

    skill_name = ""
    if domain_bench.exists():
        try:
            domain_data = json.loads(domain_bench.read_text(encoding="utf-8"))
            skill_name = domain_data.get("metadata", {}).get("skill_name", "")
        except Exception:
            pass

    detector = OverfittingDetector()
    report = detector.build_report(
        skill_name=skill_name,
        gap_data=gap_data,
    )

    if not output:
        output = domain_bench.parent / "overfitting_report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if fmt == "json":
        console.print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    else:
        gap = report.generalization_gap
        if gap:
            gap_color = "green" if gap.verdict == "good" else "yellow" if gap.verdict == "warn" else "red"
            console.print("\n[bold]Generalization Gap:[/bold]")
            console.print(f"  Domain:  +{gap.domain_delta:.2f} (ws={gap.domain_pass_rate_with:.0%}, wos={gap.domain_pass_rate_without:.0%})")
            console.print(f"  General: +{gap.general_delta:.2f} (ws={gap.general_pass_rate_with:.0%}, wos={gap.general_pass_rate_without:.0%})")
            console.print(f"  Gap:     [{gap_color}]{gap.gap:.2f}[/{gap_color}] → [{gap_color}]{gap.verdict.upper()}[/{gap_color}]")

        console.print(f"\n[bold]Overfitting Score:[/bold] {report.overfitting_score:.2f}/1.0")
        console.print(f"\n[bold]Recommendation:[/bold] {report.recommendation}")
        console.print(f"\nReport written to: {output}")

    if report.overfitting_score >= 0.7:
        raise typer.Exit(code=1)
