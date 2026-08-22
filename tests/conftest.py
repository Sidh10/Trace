"""Shared pytest fixtures — every test file in this directory gets these
automatically, no import needed.
"""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def _llm_off_by_default(monkeypatch):
    """AGENTS.md rule 2: the deterministic path is required, so it is also
    this suite's default — fast and offline. `.env` ships
    TRACE_LLM_ENABLED=true for the demo; without this fixture, every test in
    every file that doesn't explicitly pin the flag falls through to a LIVE
    Gemini call — which is what made tests/test_verify.py take ~2 minutes on
    its first run instead of under a second, before this fixture existed.

    Applies repo-wide (autouse, defined in conftest.py) rather than living in
    one test file, so a future test file doesn't need to remember to add it.
    Tests that specifically exercise the LLM path opt back in with their own
    `monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)`.
    """
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
