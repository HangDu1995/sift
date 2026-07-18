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


class CalibrationVerdict(str, Enum):
    """单次执行的过程 x 结果 coherence 判决(5 态)。
    结果(pass_rate>=0.8) x 过程(prohibition/output 硬类违规)矩阵:
    TRUSTED=结果对+过程对;HIGH_SCORE_SUSPECT=结果对+过程错;
    FIXABLE=结果错+过程对;SKILL_FAILED=结果错+过程错;PROCESS_UNKNOWN=过程无可判定。
    """
    TRUSTED = "TRUSTED"
    HIGH_SCORE_SUSPECT = "HIGH_SCORE_SUSPECT"
    FIXABLE = "FIXABLE"
    SKILL_FAILED = "SKILL_FAILED"
    PROCESS_UNKNOWN = "PROCESS_UNKNOWN"


class ProcessStatus(str, Enum):
    """过程面状态:ok=过程对;violated=有 prohibition/output 硬类违规;unknown=无可判定。"""
    OK = "ok"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


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
