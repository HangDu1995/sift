"""add — append eval cases to existing evals.json."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from skill_eval.cli.helpers import _check_claude_or_exit, _discover_evals
from skill_eval.cli.main import app, console
from skill_eval.errors import SkillEvalError
from skill_eval.models import EvalCase, EvalSet
from skill_eval.parser import parse_skill_md, resolve_skill_dir
from skill_eval.utils import call_claude


@app.command()
def add(
    skill_path: Path = typer.Argument(..., help="Path to skill directory"),
    idea: Optional[str] = typer.Option(None, "--idea", help="Natural language description of what to test. LLM generates complete eval case."),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Manual: the eval prompt"),
    expectation: Optional[list[str]] = typer.Option(None, "--expectation", help="Manual: expectation (repeatable)"),
    level: str = typer.Option("L1", "--level", help="Eval level: L1 or L2"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without saving"),
    model: Optional[str] = typer.Option(None, "--model", help="Model for LLM generation"),
):
    """Add eval cases to a skill's evals.json.

    Two modes:
    - --idea "what to test": LLM generates complete eval case from your description
    - --prompt "..." --expectation "...": manual mode, you specify the details

    Examples:
      skill-eval add pdf/ --idea "test encrypted PDF decryption"
      skill-eval add pdf/ --prompt "Decrypt secret.pdf" --expectation "produces output.pdf" --level L2
    """
    skill_path = resolve_skill_dir(skill_path)
    evals_path = _discover_evals(skill_path)
    if not evals_path:
        console.print("[red]Error:[/red] no evals.json found. Run `skill-eval init` first.")
        raise typer.Exit(code=2)

    try:
        existing = json.loads(evals_path.read_text(encoding="utf-8"))
        eval_set = EvalSet.model_validate(existing)
    except Exception:
        console.print("[red]Error:[/red] failed to read evals.json")
        raise typer.Exit(code=2)

    if idea:
        _check_claude_or_exit()
        usage: dict = {}
        start = time.perf_counter()
        new_case = _generate_from_idea(skill_path, idea, level, model, on_usage=usage.update)
        tok = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        console.print(f"耗时 [bold]{time.perf_counter() - start:.1f}s[/bold] · {tok} tok")
    elif prompt:
        expectations = list(expectation) if expectation else []
        new_case = EvalCase(
            id=0, prompt=prompt, expectations=expectations, level=level,
        )
        from skill_eval.evals.generator import EvalInitGenerator
        new_case.assertions = EvalInitGenerator._auto_generate_assertions(new_case)
    else:
        console.print("[red]Error:[/red] specify --idea or --prompt")
        raise typer.Exit(code=2)

    _display_eval(new_case)

    if dry_run:
        console.print("[dim](dry-run — not saved)[/dim]")
        return

    if not typer.confirm("\nAdd this eval to evals.json?", default=True):
        console.print("Cancelled.")
        return

    new_case.id = max((e.id for e in eval_set.evals), default=0) + 1
    eval_set.evals.append(new_case)

    data = eval_set.model_dump(mode="json")
    evals_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"[green]Added[/green] eval #{new_case.id} ({new_case.name or new_case.get_name()}) to {evals_path}")


def _generate_from_idea(skill_path: Path, idea: str, level: str, model: str | None,
                        *, on_usage=None) -> EvalCase:
    name, description, content, *_ = parse_skill_md(skill_path)
    is_l2 = level == "L2"

    prompt = (
        "You are an expert at creating targeted evaluation test cases for AI coding assistant skills.\n\n"
        f"Given a skill and a testing idea, generate ONE {'L2 (execution)' if is_l2 else 'L1 (knowledge)'} eval case.\n\n"
        f"## Skill: {name}\n{description}\n\n{content[:6000]}\n\n"
        f"## Testing idea: {idea}\n\n"
        "## Output: valid JSON only, no other text\n"
        "```json\n"
        "{\n"
        '  "name": "<level>-<short-description>",\n'
        '  "prompt": "A realistic, concrete task that a user would actually ask. '
        'Include specific file paths and parameters.",\n'
        '  "expectations": [\n'
        '    "Single, verifiable claim. GOOD: \'produces output.pdf with 3 pages\'. '
        'BAD: \'correctly processes the file\'. BAD: \'follows instructions\'."\n'
        '  ],\n'
        '  "level": "' + level + '",\n'
    )

    if is_l2:
        prompt += (
            '  "fixtures": [\n'
            '    {"path": "input-file.ext", "content": "minimal but realistic test data"}\n'
            '  ],\n'
            '  "assertions": [\n'
            '    {"name": "output file exists", "type": "FILE_EXISTS", "params": {"filename": "output.ext"}},\n'
            '    {"name": "contains expected pattern", "type": "CONTAINS_PATTERN", "params": {"pattern": "keyword", "path": "output.ext"}}\n'
            '  ]\n'
        )
    else:
        prompt += (
            '  "assertions": [\n'
            '    {"name": "mentions tool X", "type": "CONTAINS_PATTERN", "params": {"pattern": "keyword"}},\n'
            '    {"name": "avoids Y", "type": "NOT_CONTAINS", "params": {"pattern": "forbidden"}}\n'
            '  ]\n'
        )

    prompt += (
        "}\n"
        "```\n\n"
        "## Rules\n"
        "- Prompt MUST be realistic and concrete, not generic\n"
        "- Expectations MUST be objectively verifiable (no 'correctly', 'properly')\n"
        f"- {'L2: include fixtures and FILE_EXISTS assertions' if is_l2 else 'L1: no FILE_EXISTS — only text-based assertions'}\n"
        "- 3-4 good expectations, 2-3 assertions\n"
        "- Limit fixture file content to ~200 chars max\n"
    )

    try:
        response = call_claude(prompt, model=model, timeout=120, on_usage=on_usage)
    except RuntimeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=3)

    # Parse JSON from response
    import re
    match = re.search(r"\{.*\}", response, re.DOTALL)
    if not match:
        console.print("[red]Error:[/red] LLM did not return valid JSON")
        raise typer.Exit(code=3)

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        console.print("[red]Error:[/red] failed to parse LLM response")
        raise typer.Exit(code=3)

    # Build EvalCase
    case = EvalCase(
        id=0,
        name=data.get("name", ""),
        prompt=data.get("prompt", ""),
        expectations=data.get("expectations", []),
        level=data.get("level", level),
    )

    # Parse assertions
    for a in data.get("assertions", []):
        from skill_eval.models import Assertion, AssertionType
        try:
            case.assertions.append(Assertion(
                name=a.get("name", ""),
                type=AssertionType(a.get("type", "CONTAINS_PATTERN")),
                params=a.get("params", {}),
            ))
        except (ValueError, KeyError):
            continue

    # Parse fixtures
    for f in data.get("fixtures", []):
        from skill_eval.models import FixtureFile
        case.fixtures.append(FixtureFile(
            path=f.get("path", ""),
            content=f.get("content", ""),
        ))

    # Auto-add missing assertions
    from skill_eval.evals.generator import EvalInitGenerator
    case.assertions = EvalInitGenerator._auto_generate_assertions(case)

    return case


def _display_eval(case: EvalCase) -> None:
    console.print(f"\n[bold]{case.get_name()}[/bold] ([{case.level}])")
    console.print(f"  Prompt: [dim]{case.prompt[:200]}{'...' if len(case.prompt) > 200 else ''}[/dim]")
    if case.expectations:
        console.print(f"  Expectations ({len(case.expectations)}):")
        for e in case.expectations:
            console.print(f"    • {e}")
    if case.assertions:
        types = set(a.type.value for a in case.assertions)
        console.print(f"  Assertions ({len(case.assertions)}): [dim]{', '.join(sorted(types))}[/dim]")
    if case.fixtures:
        console.print(f"  Fixtures ({len(case.fixtures)}):")
        for f in case.fixtures:
            preview = f.content[:60].replace("\n", " ")
            console.print(f"    • {f.path} [dim]({len(f.content)} chars: {preview}...)[/dim]")
