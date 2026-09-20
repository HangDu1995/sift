"""Disable retry waits for isolated auditor unit tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_llm_retry_sleep(no_llm_retry_wait) -> None:
    """审计判官单测统一选择无退避等待模式。"""
