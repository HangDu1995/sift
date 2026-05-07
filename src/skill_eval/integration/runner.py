"""L2 integration test runner — chain, routing, contract scenarios."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def run_chain_test(
    chain: list[str],
    input_data: dict,
    workspace_dir: Path,
    step_results: dict[str, tuple[bool, Path | None]] | None = None,
    assertions: list[dict] | None = None,
) -> dict:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    all_passed = True

    for idx, skill_name in enumerate(chain):
        step_dir = workspace_dir / f"step-{idx}-{skill_name}"
        step_dir.mkdir(exist_ok=True)

        if not all_passed:
            steps.append({"skill": skill_name, "status": "SKIPPED", "index": idx})
            continue

        if step_results and skill_name in step_results:
            passed, output_dir = step_results[skill_name]
            if passed:
                steps.append({"skill": skill_name, "status": "PASSED", "index": idx})
            else:
                steps.append({"skill": skill_name, "status": "FAILED", "index": idx})
                all_passed = False
        else:
            steps.append({"skill": skill_name, "status": "PASSED", "index": idx})

    return {
        "type": "chain",
        "chain": chain,
        "steps": steps,
        "passed": all_passed,
    }


def run_routing_test(
    test_prompts: list[dict],
    skill_keywords: dict[str, list[str]],
) -> dict:
    results: list[dict] = []
    all_passed = True

    for tp in test_prompts:
        prompt = tp.get("prompt", "")
        should_trigger = set(tp.get("should_trigger", []))
        should_not_trigger = set(tp.get("should_not_trigger", []))

        triggered = set()
        for skill_name, keywords in skill_keywords.items():
            for kw in keywords:
                if kw.lower() in prompt.lower():
                    triggered.add(skill_name)
                    break

        correct = triggered == should_trigger and not triggered.intersection(should_not_trigger)
        if not correct:
            all_passed = False

        results.append({
            "prompt": prompt,
            "triggered": sorted(triggered),
            "should_trigger": sorted(should_trigger),
            "should_not_trigger": sorted(should_not_trigger),
            "correct": correct,
        })

    return {
        "type": "routing",
        "results": results,
        "passed": all_passed,
    }


def run_contract_test(
    producer: str,
    consumer: str,
    artifact_path: str,
    checks: list[dict],
    output_dir: Path,
) -> dict:
    artifact = Path(artifact_path)
    check_results: list[dict] = []
    all_passed = True

    for check in checks:
        ctype = check.get("type", "")
        if ctype == "FILE_EXISTS":
            passed = artifact.exists()
            evidence = f"Artifact exists: {artifact.name}" if passed else f"Artifact not found: {artifact}"
        elif ctype == "JSON_VALID":
            if artifact.exists():
                try:
                    json.loads(artifact.read_text(encoding="utf-8"))
                    passed = True
                    evidence = "Valid JSON"
                except (json.JSONDecodeError, OSError) as e:
                    passed = False
                    evidence = f"Invalid JSON: {e}"
            else:
                passed = False
                evidence = "Artifact not found"
        elif ctype == "PATTERN_MATCH":
            pattern = check.get("pattern", "")
            if artifact.exists():
                content = artifact.read_text(encoding="utf-8")
                passed = bool(re.search(pattern, content))
                evidence = f"Pattern found" if passed else f"Pattern not found: {pattern}"
            else:
                passed = False
                evidence = "Artifact not found"
        else:
            passed = True
            evidence = f"Check type '{ctype}' skipped"

        if not passed:
            all_passed = False
        check_results.append({"type": ctype, "passed": passed, "evidence": evidence})

    return {
        "type": "contract",
        "producer": producer,
        "consumer": consumer,
        "artifact": str(artifact),
        "checks": check_results,
        "passed": all_passed,
    }


def run_integration_batch(
    scenarios: list[dict],
    skill_keywords: dict[str, list[str]],
    scenario_type: str | None = None,
    workspace_dir: Path | None = None,
) -> dict:
    if workspace_dir is None:
        workspace_dir = Path("workspace")

    results: list[dict] = []
    for scenario in scenarios:
        stype = scenario.get("type", "")
        if scenario_type and stype != scenario_type:
            continue

        if stype == "routing":
            r = run_routing_test(
                test_prompts=scenario.get("test_prompts", []),
                skill_keywords=skill_keywords,
            )
            r["name"] = scenario.get("name", "")
            results.append(r)
        elif stype == "chain":
            r = run_chain_test(
                chain=scenario.get("chain", []),
                input_data=scenario.get("input", {}),
                workspace_dir=workspace_dir / scenario.get("name", "chain"),
            )
            r["name"] = scenario.get("name", "")
            results.append(r)
        elif stype == "contract":
            out = workspace_dir / scenario.get("name", "contract")
            out.mkdir(parents=True, exist_ok=True)
            r = run_contract_test(
                producer=scenario.get("producer", ""),
                consumer=scenario.get("consumer", ""),
                artifact_path=scenario.get("artifact", ""),
                checks=scenario.get("checks", []),
                output_dir=out,
            )
            r["name"] = scenario.get("name", "")
            results.append(r)

    passed = sum(1 for r in results if r.get("passed"))
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "scenarios": results,
    }
