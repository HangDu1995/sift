"""LLM-driven eval-config.yaml generator — scans skills metadata, builds prompt, generates config."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import yaml

from skill_eval.utils import call_claude


def scan_skills_metadata(root_dir: Path | str) -> dict:
    root = Path(root_dir)
    skills: list[dict] = []
    domains: set[str] = set()
    types: set[str] = set()
    collection_prefixes: set[str] = set()

    if not root.is_dir():
        return {"total_skills": 0, "collection_prefixes": [], "domains": [], "types": [], "skills": skills}

    candidate_dirs: list[tuple[str, Path]] = []

    if (root / "skills").is_dir():
        candidate_dirs.append((root.name, root / "skills"))

    for entry in sorted(root.iterdir()):
        skills_dir = entry / "skills"
        if skills_dir.is_dir() and (entry / "skills") not in [d for _, d in candidate_dirs]:
            parts = entry.name.split("-")
            collection_prefixes.add(parts[0] + "-*" if parts else entry.name)
            candidate_dirs.append((entry.name, skills_dir))

    for col_name, skills_dir in candidate_dirs:
        for skill_entry in sorted(skills_dir.iterdir()):
            skill_dir = skill_entry
            if not skill_dir.is_dir():
                continue
            fm = _extract_frontmatter(skill_dir / "SKILL.md")
            name = fm.get("name", skill_entry.name)
            stype = fm.get("type", "domain")
            domain = fm.get("domain", "general")
            desc = fm.get("description", "")
            tags = fm.get("tags", [])
            domains.add(domain)
            types.add(stype)
            skills.append({
                "name": name, "type": stype, "domain": domain,
                "description": desc[:200], "tags": tags,
            })

    return {
        "total_skills": len(skills),
        "collection_prefixes": sorted(collection_prefixes),
        "domains": sorted(domains),
        "types": sorted(types),
        "skills": skills,
    }


class ConfigGenerator:
    def build_prompt(self, metadata: dict) -> str:
        skills_json = json.dumps(metadata, indent=2, ensure_ascii=False)
        domains = metadata.get("domains", [])
        types = metadata.get("types", [])
        prefixes = metadata.get("collection_prefixes", [])

        domains_hint = ", ".join(domains) if domains else "(detect from skills)"
        types_hint = ", ".join(types) if types else "(detect from skills)"
        prefix_hint = prefixes[0] if prefixes else "(detect from directory names)"

        return textwrap.dedent(f"""\
            You are a configuration generator for a skill evaluation framework.

            Given the skill metadata (JSON below), generate an `eval-config.yaml` file.

            ## Available assertion types

            Programmatic (deterministic, fast):
            - `FILE_EXISTS`: check files exist (params: filename)
            - `CONTAINS_PATTERN`: regex match in files (params: pattern, path)
            - `NOT_CONTAINS`: regex must NOT match (params: pattern, path)
            - `JSON_HAS_FIELD`: check JSON field exists (params: filename, field)
            - `LINE_COUNT`: check line count range (params: min, max, path)
            - `CUSTOM_SCRIPT`: run custom Python script (params: script, args)
            - `CROSS_FILE_CONSISTENCY`: check imports resolve (params: include)
            - `CODE_COMPILES`: basic syntax check (params: include)
            - `CUSTOM_PLUGIN`: domain-specific plugin (params: plugin_type)

            LLM-based:
            - `llm_judge`: LLM quality judgment (params: criteria)

            ## YAML structure

            ```yaml
            version: 1
            scanning:
              collection_prefix: "<prefix>"

            domain_templates:
              <domain>:
                assertions:
                  - type: <ASSERTION_TYPE>
                    pattern: "<regex>"
                    description: "<what it checks>"

            type_templates:
              <type>:
                assertions:
                  - type: <ASSERTION_TYPE>
                    description: "<desc>"

            integration_scenarios:
              - name: "<name>"
                type: chain|routing|contract
                # chain: chain (list), input (dict)
                # routing: test_prompts (list of {{prompt, should_trigger, should_not_trigger}})
                # contract: producer, consumer, artifact, checks (list)

            skill_keywords:
              <skill_name>: ["keyword1", "keyword2"]

            pipelines:
              - name: "<pipeline-name>"
                description: "<what this pipeline tests>"
                project: "fixtures/projects/example"
                steps:
                  - name: "<step-name>"
                    skill: "<skill-name>"
                    prompt: "<prompt to send to claude>"
                    expectations: ["<expectation>"]
                    assertions:
                      - name: "<assertion>"
                        type: <ASSERTION_TYPE>
                        params: {{}}
                    depends_on: ["<previous-step>"]
            ```

            ## Rules

            1. **scanning.collection_prefix**: `{prefix_hint}`
            2. **domain_templates**: One entry per domain. Domains: {domains_hint}.
               For each domain, write 3-6 assertions derived from skill descriptions.
            3. **type_templates**: One entry per type. Types: {types_hint}.
               Write 2-4 assertions per type.
            4. **integration_scenarios**: Generate 2-4 scenarios:
               - At least 1 chain (sequence of skills)
               - At least 1 routing (keyword-based dispatch test)
               - At least 1 contract (producer-consumer artifact check)
            5. **skill_keywords**: For each skill, extract 5-8 keywords from its description.
            6. **pipelines**: Generate 1 pipeline that chains all skills in dependency order.
               Each step should have a realistic prompt, 1-2 expectations, and 1-2 assertions.
               Use depends_on to create the dependency chain.
               Use a placeholder project path like "fixtures/projects/example" that users can replace.
            7. Output ONLY the YAML. No explanation, no markdown fences.

            ## Skill Metadata

            ```json
            {skills_json}
            ```
            """)

    @staticmethod
    def build_pipeline_from_deps(
        skills: list[dict],
        dep_graph: dict[str, list[str]] | None = None,
        project: str = "fixtures/projects/example",
    ) -> list[dict]:
        if not skills:
            return []

        skill_names = [s["name"] for s in skills]
        graph = dep_graph or {}

        visited: set[str] = set()
        order: list[str] = []

        def visit(name: str) -> None:
            if name in visited or name not in skill_names:
                return
            visited.add(name)
            for dep in graph.get(name, []):
                visit(dep)
            order.append(name)

        for name in skill_names:
            visit(name)

        skill_map = {s["name"]: s for s in skills}
        steps: list[dict] = []
        for idx, name in enumerate(order):
            s = skill_map.get(name, {})
            deps = graph.get(name, [])
            steps.append({
                "name": name.split("-")[-1] if "-" in name else name,
                "skill": name,
                "prompt": f"Execute {name} on the project",
                "expectations": [],
                "assertions": [],
                "depends_on": deps,
            })

        return [{
            "name": f"{skills[0]['domain'] if skills else 'default'}-full-flow",
            "description": "Auto-generated pipeline from skill dependency graph",
            "project": project,
            "steps": steps,
        }]

    def generate(
        self,
        root_dir: Path | str,
        output_path: Path | str | None = None,
        model: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        metadata = scan_skills_metadata(root_dir)
        if metadata["total_skills"] == 0:
            raise ValueError(f"No skills found in {root_dir}")

        prompt = self.build_prompt(metadata)

        if dry_run:
            return {"dry_run": True, "metadata": metadata, "prompt_length": len(prompt)}

        raw = call_claude(prompt, model=model, timeout=300)
        cleaned = _clean_yaml_output(raw)
        parsed = yaml.safe_load(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError(f"LLM output is not a valid YAML dict: {cleaned[:200]}")

        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(yaml.dump(parsed, allow_unicode=True, default_flow_style=False, sort_keys=False),
                           encoding="utf-8")

        return parsed


def _extract_frontmatter(md_path: Path) -> dict:
    if not md_path.exists():
        return {}
    try:
        content = md_path.read_text(encoding="utf-8").lstrip("\ufeff")
    except OSError:
        return {}
    if not content.startswith("---"):
        return {}
    rest = content[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}
    try:
        parsed = yaml.safe_load(rest[:end])
        return parsed if isinstance(parsed, dict) else {}
    except yaml.YAMLError:
        return {}


def _clean_yaml_output(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```yaml"):
        text = text[len("```yaml"):]
    elif text.startswith("```yml"):
        text = text[len("```yml"):]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
