"""Shared literal prompt sections; formatting is owned by each caller."""

EVAL_JSON_HEADER = (
    "\n"
    "## Output JSON (ONLY this, no other text)\n"
    "```json\n"
    "{{\n"
    '  "skill_name": "{name}",\n'
    '  "evals": [\n'
    "    {{\n"
    '      "id": 1,\n'
    '      "name": "L1-<category>-<short>",\n'
    '      "level": "L1",\n'
)

EVAL_RATIONALE_RULE = (
    '      "confidence": 0.0-1.0\n'
    "    }}\n"
    "  ]\n"
    "}}\n"
    "```\n"
    "\n"
    "## Rules\n"
    "- Every eval MUST include `rationale`: cite the exact SKILL.md span it tests (section / bullet "
    "/ quoted rule, same language as SKILL.md). `confidence` (0.0-1.0) = how sure you are this eval "
    "cleanly tests that rule (for human review triage).\n"
)

SKILL_SOURCE_SECTION = "\n## Skill\n\nname: {name}\ndescription: {description}\n\n{content}\n"

AGENT_SOURCE_HEADER = "\n## Agent\nname: {name}\ndescription: {description}\n\n## Agent system prompt(角色指令正文)\n"
