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


@pytest.fixture(autouse=True)
def _reset_approval_threshold_after_each_test():
    """`config.TRACE_APPROVAL_THRESHOLD` is the one other module-level config
    value the app mutates directly at runtime, not just reads (grepped
    `app/` for `config\\.[A-Z_]+\\s*=` — the only hits are
    `app/environment/routes.py`'s `/environment/reset` and
    `exceeds_approval`'s scenario handler). A plain `monkeypatch.setattr`
    *before* a test doesn't protect against the test's OWN code — or the
    real endpoint it calls — reassigning it mid-test, the way
    `_llm_off_by_default` above protects `TRACE_LLM_ENABLED`.

    Caught concretely: a test exercising `exceeds_approval` through the real
    `TestClient`, without resetting the environment afterward, left this at
    50,000 for the rest of the pytest session — cascading into 14 unrelated
    failures across `test_solver.py` and `test_staleness.py` whose own
    scenarios assumed the real default. Generalized here the same way the
    LLM-flag leak was generalized, so the next test that forgets to reset
    doesn't reintroduce it."""
    yield
    config.TRACE_APPROVAL_THRESHOLD = config.DEFAULT_APPROVAL_THRESHOLD


# --------------------------------------------------------------------------
# Shared helper for term-absence assertions
# --------------------------------------------------------------------------
# Three separate tests in this repo have asserted "term X does not appear in
# module Y" and been tripped by the module's own docstring EXPLAINING why X
# is absent — `mark_po_delayed`, `allocate_stock`, `interval`. Each was fixed
# in place and the next one hit it again, so here is the shared version.
#
# Import it as `from conftest import executable_source` (pytest puts the
# conftest directory on sys.path).


def executable_source(path: str) -> str:
    """Return `path`'s source with every docstring stripped.

    Prose that explains why something is NOT done is documentation, not an
    occurrence of the thing. Assert term-absence against this, not against
    the raw file — otherwise a well-documented decision fails its own test.
    """
    import ast

    tree = ast.parse(open(path, encoding="utf-8").read())
    for scope in ast.walk(tree):
        if not isinstance(
            scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = scope.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            scope.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))
