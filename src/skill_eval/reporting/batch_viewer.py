"""Batch report generator — produces combined HTML from multi-skill evaluation."""

from __future__ import annotations

import json
from pathlib import Path


def collect_batch_data(workspace: Path, manifest: dict | None = None) -> dict:
    """Collect all benchmark data from a batch workspace."""
    skills: list[dict] = []

    cascade_path = workspace / "cascade-report.json"
    cascade = None
    if cascade_path.exists():
        try:
            cascade = json.loads(cascade_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if manifest is None:
        try:
            cascade_data = json.loads(cascade_path.read_text(encoding="utf-8")) if cascade_path.exists() else {}
        except Exception:
            cascade_data = {}
        manifest = cascade_data.get("manifest") if cascade_data else None

    unit_dir = workspace / "unit"
    if unit_dir.is_dir():
        for skill_dir in sorted(unit_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            iteration = skill_dir / "iteration-1"
            bench_path = iteration / "benchmark.json"
            if not bench_path.exists():
                continue

            try:
                bench = json.loads(bench_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            meta = bench.get("metadata", {})
            summary = bench.get("run_summary", {})
            runs = bench.get("runs", [])
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
                "delta_time": delta.get("time_seconds"),
                "delta_tokens": delta.get("tokens"),
                "runs": runs,
                "benchmark": bench,
                "has_comparisons": bool(bench.get("comparisons")),
                "has_analysis": bool(bench.get("analysis")),
                "notes_count": len(bench.get("notes", [])),
                "dependencies": [],
                "depended_by": [],
                "overfitting": _load_overfitting_report(iteration),
            }
            skills.append(skill_data)

    if cascade:
        attributions = cascade.get("attributions", {})
        for s in skills:
            attr = attributions.get(s["name"], "PASSED")
            s["status"] = attr

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
        "cascade": cascade,
        "manifest": manifest,
    }




def _load_overfitting_report(iteration_dir: Path) -> dict | None:
    path = iteration_dir / "overfitting_report.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def generate_batch_html(workspace: Path, manifest: dict | None = None) -> str:
    template_path = Path(__file__).parent / "templates" / "batch-report.html"
    if not template_path.exists():
        return "<html><body><h1>Error: batch-report.html template not found</h1></body></html>"

    template = template_path.read_text(encoding="utf-8")
    data = collect_batch_data(workspace, manifest)
    json_data = json.dumps(data, ensure_ascii=False)
    html = template.replace("/*__EMBEDDED_DATA__*/", f"const EMBEDDED_DATA = {json_data};")
    return html
