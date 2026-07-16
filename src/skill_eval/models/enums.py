"""Enums for skill-eval."""

from __future__ import annotations

from enum import Enum


class AssertionTier(str, Enum):
    TIER1 = "TIER1"  # Deterministic: file/regex checks
    TIER3 = "TIER3"  # Semantic: LLM-judged quality/fact checks


class AssertionType(str, Enum):
    # Tier 1 — Deterministic
    FILE_EXISTS = "FILE_EXISTS"
    FILE_CONTAINS = "FILE_CONTAINS"
    CONTAINS_PATTERN = "CONTAINS_PATTERN"
    JSON_HAS_FIELD = "JSON_HAS_FIELD"
    LINE_COUNT = "LINE_COUNT"
    NOT_CONTAINS = "NOT_CONTAINS"
    CUSTOM_SCRIPT = "CUSTOM_SCRIPT"
    CROSS_FILE_CONSISTENCY = "CROSS_FILE_CONSISTENCY"
    CODE_COMPILES = "CODE_COMPILES"
    CUSTOM_PLUGIN = "CUSTOM_PLUGIN"
    TOOL_CALLED = "TOOL_CALLED"
    STEP_COUNT = "STEP_COUNT"
    # Tier 3 — Semantic (LLM-judged)
    LLM_GRADE = "LLM_GRADE"
    LLM_FACT = "LLM_FACT"
    LLM_NOT_DELEGATE = "LLM_NOT_DELEGATE"


class AssertionVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNEVAL = "UNEVAL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


class InstructionSource(str, Enum):
    HEADING_STEP = "HEADING_STEP"
    TOOL_REQUIREMENT = "TOOL_REQUIREMENT"
    CONSTRAINT = "CONSTRAINT"
    ANTI_FABRICATION = "ANTI_FABRICATION"
    OUTPUT_FORMAT = "OUTPUT_FORMAT"
    REFERENCE_USAGE = "REFERENCE_USAGE"


class InstructionPriority(str, Enum):
    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"


class CheckMethod(str, Enum):
    PATTERN_MATCH = "PATTERN_MATCH"
    TOOL_CALL_CHECK = "TOOL_CALL_CHECK"
    FILE_PRESENCE = "FILE_PRESENCE"
    LLM_JUDGE = "LLM_JUDGE"


class ConfidenceGrade(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNRELIABLE = "UNRELIABLE"


class SignalType(str, Enum):
    HIGH_AGREEMENT_RATE = "HIGH_AGREEMENT_RATE"
    LOW_AGREEMENT_RATE = "LOW_AGREEMENT_RATE"
    COMPLIANCE_BACKS_SCORING = "COMPLIANCE_BACKS_SCORING"
    COMPLIANCE_CONTRADICTS_SCORING = "COMPLIANCE_CONTRADICTS_SCORING"
    WEAK_EVIDENCE = "WEAK_EVIDENCE"
    GRADER_SKIPPED_CHECKS = "GRADER_SKIPPED_CHECKS"
    SUPERFICIAL_PASS = "SUPERFICIAL_PASS"


class ResolutionStrategy(str, Enum):
    PROGRAMMATIC_CORRECT = "PROGRAMMATIC_CORRECT"
    LLM_CORRECT = "LLM_CORRECT"
    AMBIGUOUS = "AMBIGUOUS"
    BOTH_WRONG = "BOTH_WRONG"


class SkillCategory(str, Enum):
    CAPABILITY_UPLIFT = "CAPABILITY_UPLIFT"
    ENCODED_PREFERENCE = "ENCODED_PREFERENCE"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class SecuritySeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SecurityVerdict(str, Enum):
    SAFE = "SAFE"
    REVIEW_NEEDED = "REVIEW_NEEDED"
    BLOCKED = "BLOCKED"


class ScannerStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"
