"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_SKILL_DIR = FIXTURES_DIR / "sample-skill"


@pytest.fixture
def sample_skill_path() -> Path:
    return SAMPLE_SKILL_DIR


@pytest.fixture
def sample_evals_path() -> Path:
    return FIXTURES_DIR / "sample-evals.json"


@pytest.fixture
def sample_evals_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample-evals.json").read_text(encoding="utf-8"))


@pytest.fixture
def sample_grading_data() -> dict:
    return json.loads((FIXTURES_DIR / "sample-grading.json").read_text(encoding="utf-8"))
