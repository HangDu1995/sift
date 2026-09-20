"""Typed source and evidence records shared by audit test factories."""

from typing import NamedTuple

from sift.models.audit_models import AuditCategory, AuditMethod


class FindingOrigin(NamedTuple):
    source: str = "CONSTRAINT"
    category: AuditCategory = AuditCategory.PROHIBITION
    seq: int = 0
    section: str = ""
    derived: bool = False
    control_flow: str = ""
    excerpt: str = ""
    synthetic: bool = False


class FindingEvidence(NamedTuple):
    method: AuditMethod = AuditMethod.PROGRAMMATIC
    text: str = "e"
    related: list[str] | None = None


class ScoringRates(NamedTuple):
    programmatic: float | None = None
    llm: float | None = None
