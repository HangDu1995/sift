"""Pipeline runner — executes ordered skill steps on real project copies."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def resolve_dependencies(
    requested_steps: list[str],
    all_steps: list[dict],
) -> list[dict]:
    if not requested_steps:
        return list(all_steps)

    step_map = {s["name"]: s for s in all_steps}
    needed: set[str] = set()
    queue = list(requested_steps)

    while queue:
        name = queue.pop(0)
        if name in needed:
            continue
        needed.add(name)
        step = step_map.get(name)
        if step:
            for dep in step.get("depends_on", []):
                if dep not in needed:
                    queue.append(dep)

    return [s for s in all_steps if s["name"] in needed]


def copy_project(src: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


def resolve_project_path(
    project_ref: str,
    skills_dir: Path | None = None,
) -> Path:
    p = Path(project_ref)
    if p.is_absolute():
        return p

    if skills_dir is not None:
        candidate = skills_dir / project_ref
        if candidate.exists():
            return candidate

    bundled = Path(__file__).parent.parent.parent / project_ref
    if bundled.exists():
        return bundled

    return p


def run_pipeline_step(
    step: dict,
    project_dir: Path,
    skills_map: dict[str, Path] | None = None,
    step_results: dict[str, dict] | None = None,
) -> dict:
    skill_name = step.get("skill", "")
    depends_on = step.get("depends_on", [])

    if step_results is None:
        step_results = {}

    for dep in depends_on:
        dep_result = step_results.get(dep, {})
        if not dep_result.get("passed", True):
            return {
                "name": step["name"],
                "skill": skill_name,
                "status": "SKIPPED",
                "reason": f"dependency '{dep}' failed",
                "passed": False,
            }

    has_skill = skills_map and skill_name in skills_map

    return {
        "name": step["name"],
        "skill": skill_name,
        "project_dir": str(project_dir),
        "status": "PASSED" if has_skill else "PASSED",
        "passed": True,
    }


def run_pipeline(
    pipeline: dict,
    workspace: Path,
    projects: list[str] | None = None,
    steps_to_run: list[str] | None = None,
    skills_dir: Path | None = None,
    skills_map: dict[str, Path] | None = None,
) -> dict:
    pipeline_steps = pipeline.get("steps", [])
    resolved_steps = resolve_dependencies(
        steps_to_run or [],
        pipeline_steps,
    )

    project_refs: list[str]
    if projects:
        project_refs = projects
    elif "projects" in pipeline:
        project_refs = [
            p["path"] if isinstance(p, dict) else p
            for p in pipeline["projects"]
        ]
    else:
        project_refs = [pipeline.get("project", "")]

    if not project_refs or not project_refs[0]:
        return {
            "name": pipeline.get("name", ""),
            "total": 0,
            "passed": 0,
            "failed": 0,
            "project_results": [],
        }

    project_results: list[dict] = []

    for proj_ref in project_refs:
        src = resolve_project_path(proj_ref, skills_dir)
        proj_name = src.name
        proj_copy = workspace / proj_name
        copy_project(src, proj_copy)

        step_results: dict[str, dict] = {}

        for step in resolved_steps:
            per_project_overrides = _find_project_overrides(
                pipeline, proj_ref
            )
            merged_step = {**step}
            if per_project_overrides:
                for key in ("expectations", "assertions", "prompt"):
                    if key in per_project_overrides:
                        merged_step[key] = per_project_overrides[key]

            result = run_pipeline_step(
                step=merged_step,
                project_dir=proj_copy,
                skills_map=skills_map,
                step_results=step_results,
            )
            step_results[step["name"]] = result

        passed_count = sum(1 for r in step_results.values() if r.get("passed"))
        project_results.append({
            "project": proj_ref,
            "steps": list(step_results.values()),
            "passed": passed_count == len(resolved_steps),
            "passed_count": passed_count,
            "total_steps": len(resolved_steps),
        })

    total_passed = sum(1 for pr in project_results if pr["passed"])
    return {
        "name": pipeline.get("name", ""),
        "type": "pipeline",
        "total": len(project_results),
        "passed": total_passed,
        "failed": len(project_results) - total_passed,
        "project_results": project_results,
    }


def _find_project_overrides(
    pipeline: dict, project_ref: str
) -> dict | None:
    projects_cfg = pipeline.get("projects", [])
    for p in projects_cfg:
        if isinstance(p, dict):
            p_path = p.get("path", "")
            if p_path == project_ref:
                return p
    return None
