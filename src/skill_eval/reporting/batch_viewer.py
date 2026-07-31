"""Batch report generator — produces combined HTML from multi-skill evaluation."""

from __future__ import annotations

import json
from pathlib import Path


def _is_flat_delta(value) -> bool:
    """客观 Δ |≤0.05 = with/without 趋零(skill 相对 base 无增益,可能已被模型原生能力覆盖)。

    与 viewer.html 的 ``isFlatDelta`` 同逻辑/同阈值(0.05,对齐 dClr 染色边界)。
    None/缺值/非数 → False(诚实,不报假趋零)。客观为主(稳),不在此判主观(漂)。
    """
    if value is None:
        return False
    try:
        n = float(value)
    except (TypeError, ValueError):
        return False
    return abs(n) <= 0.05


def collect_batch_data(workspace: Path, manifest: dict | None = None) -> dict:
    """Collect all benchmark data from a batch workspace."""
    skills: list[dict] = []

    unit_dir = workspace / "unit"
    if unit_dir.is_dir():
        for skill_dir in sorted(unit_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            bench_path = skill_dir / "benchmark.json"
            if not bench_path.exists():
                continue

            try:
                bench = json.loads(bench_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            meta = bench.get("metadata", {})
            summary = bench.get("run_summary", {})
            delta = summary.get("delta", {})

            with_skill = summary.get("with_skill", {}).get("pass_rate", {})
            without_skill = summary.get("without_skill", {}).get("pass_rate", {})

            skill_data = {
                "name": meta.get("skill_name", skill_dir.name),
                "path": str(skill_dir),
                "status": "PASSED",
                "pass_rate_with": with_skill.get("mean") if with_skill else None,
                "pass_rate_without": without_skill.get("mean") if without_skill else None,
                "delta": delta.get("pass_rate"),
                "delta_programmatic": delta.get("pass_rate_programmatic"),
                "delta_time": delta.get("time_seconds"),
                "delta_tokens": delta.get("tokens"),
                "flat_programmatic": _is_flat_delta(delta.get("pass_rate_programmatic")),
                "runs": bench.get("runs", []),
                "benchmark": bench,
                "has_comparisons": bool(bench.get("comparisons")),
                "has_analysis": bool(bench.get("analysis")),
                "notes_count": len(bench.get("notes", [])),
                "dependencies": [],
                "depended_by": [],
            }
            skills.append(skill_data)

    if manifest:
        manifest_skills = manifest.get("skills", [])
        dep_map = {}
        for ms in manifest_skills:
            path_val = ms.get("path", "")
            dep_map[path_val] = ms.get("depends_on", [])

        for s in skills:
            s["dependencies"] = dep_map.get(s["name"], []) or dep_map.get(s["path"], [])

        for s in skills:
            for dep_name in s.get("dependencies", []):
                for other in skills:
                    if other["name"] == dep_name or other["path"] == dep_name:
                        other.setdefault("depended_by", []).append(s["name"])
                        break

    return {
        "skills": skills,
        "manifest": manifest,
    }


def generate_batch_html(workspace: Path, manifest: dict | None = None) -> str:
    template_path = Path(__file__).parent / "templates" / "batch-report.html"
    if not template_path.exists():
        return "<html><body><h1>Error: batch-report.html template not found</h1></body></html>"

    template = template_path.read_text(encoding="utf-8")
    data = collect_batch_data(workspace, manifest)
    json_data = json.dumps(data, ensure_ascii=False)
    html = template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {json_data};")
    return html
