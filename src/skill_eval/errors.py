"""Custom exceptions and error codes for skill-eval."""

from __future__ import annotations


class SkillEvalError(Exception):
    exit_code: int = 1


class InputError(SkillEvalError):
    exit_code: int = 2


class ExecutionError(SkillEvalError):
    exit_code: int = 3


class SkillParseError(InputError):
    pass


class EvalValidationError(InputError):
    pass


class ManifestError(InputError):
    pass


class CyclicDependencyError(InputError):
    pass


class ClaudeRunnerError(ExecutionError):
    pass


class ScoringError(ExecutionError):
    pass


class ComplianceError(ExecutionError):
    pass


class CalibrationError(ExecutionError):
    pass


class ReportError(ExecutionError):
    pass


class OpencodeDbError(ExecutionError):
    """opencode .db → export 流程失败(binary 缺失 / 非零退出 / 超时 / 无 session / id 不匹配)。"""
    pass
