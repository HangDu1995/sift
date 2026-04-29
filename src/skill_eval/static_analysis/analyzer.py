"""Static analysis engine — runs all checks and produces scorecard."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from skill_eval.models import AnalysisReport, CheckResult, SkillCategory
from skill_eval.parser import parse_skill_md
from skill_eval.static_analysis.checks import ALL_CHECKS
from skill_eval.static_analysis.classifier import classify_skill


class StaticAnalyzer:
    def analyze(self, skill_path: Path) -> AnalysisReport:
        name, description, content, frontmatter, body = parse_skill_md(skill_path)

        checks: list[CheckResult] = []
        for check_type, check_fn in ALL_CHECKS:
            if check_type == "frontmatter":
                result = check_fn(frontmatter)
            elif check_type == "content":
                result = check_fn(content)
            elif check_type == "body":
                result = check_fn(body)
            elif check_type == "path":
                result = check_fn(skill_path)
            else:
                continue
            checks.append(result)

        category = classify_skill(body, description)

        return AnalysisReport(
            skill_name=name,
            skill_path=str(skill_path),
            category=category,
            checks=checks,
            score=sum(1 for c in checks if c.passed),
            max_score=len(checks),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
