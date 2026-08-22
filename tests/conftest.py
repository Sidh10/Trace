"""Shared pytest fixtures — every test file in this directory gets these
automatically, no import needed.
"""

from __future__ import annotations

import socket

import pytest

from app import config

# --------------------------------------------------------------------------
# Outbound-network kill switch — structural, repo-wide
# --------------------------------------------------------------------------
# Three times in this project's history a test set TRACE_LLM_ENABLED=True and
# mocked only ONE of the two LLM entry points (`parse_supplier_claim` /
# `narrate_decision`), so the unmocked one made a real Gemini call from inside
# the suite: ~2 minutes of wall time and free-tier quota burned, and the suite
# still passed, so nothing surfaced it. Fixing that per-file kept failing
# because the next new test file reintroduces it.
#
# This makes a mock gap FAIL LOUDLY instead of leaking. It is session-scoped
# and autouse, so no test file has to remember anything.
#
# Loopback is deliberately allowed, not blocked: on Windows, asyncio's
# ProactorEventLoop builds its self-pipe with `socket.socketpair()`, which
# falls back to a real 127.0.0.1 connect(). Blocking that breaks
# fastapi.testclient.TestClient outright — the orchestrator's own HTTP tests
# cannot run at all. Blocking only non-loopback destinations catches every
# real outbound call (Gemini included) while leaving in-process ASGI intact.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "", None})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


class OutboundNetworkBlocked(RuntimeError):
    """Raised instead of letting a test reach the network. If you are seeing
    this, a test enabled an LLM path without mocking every entry point it
    reaches — mock it, don't disable this guard."""


def _destination_host(address):
    if isinstance(address, (tuple, list)) and address:
        return address[0]
    return None  # AF_UNIX and friends — not an outbound network call


def _guard(original):
    def wrapped(self, address, *args, **kwargs):
        host = _destination_host(address)
        if host not in _LOOPBACK_HOSTS:
            raise OutboundNetworkBlocked(
                f"Outbound network call to {host!r} blocked during the test "
                "suite. A test reached a real network endpoint — almost "
                "certainly an LLM entry point that wasn't mocked. Mock it "
                "(see tests/test_ratchet.py for the pattern); do not disable "
                "this guard."
            )
        return original(self, address, *args, **kwargs)

    return wrapped


@pytest.fixture(scope="session", autouse=True)
def _block_outbound_network():
    socket.socket.connect = _guard(_real_connect)
    socket.socket.connect_ex = _guard(_real_connect_ex)

    def blocked_create_connection(address, *args, **kwargs):
        host = _destination_host(address)
        if host not in _LOOPBACK_HOSTS:
            raise OutboundNetworkBlocked(
                f"Outbound network call to {host!r} blocked during the test suite."
            )
        return _real_create_connection(address, *args, **kwargs)

    socket.create_connection = blocked_create_connection
    try:
        yield
    finally:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        socket.create_connection = _real_create_connection


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
    `monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)` — and must mock
    every entry point they reach, or `_block_outbound_network` above fails
    them.
    """
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
