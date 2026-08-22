"""Environment-variable configuration — AGENTS.md rule 2, ARCHITECTURE.md §8.

Nothing here does I/O beyond reading env vars. `.env` (gitignored) is loaded
once, here, before any other app module needs a setting.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return float(raw)


# AGENTS.md rule 2 — master switch. Everything downstream must keep working
# with this False: regex/keyword parsing for supplier messages, template
# strings for narration. The LLM is bolted on top, never load-bearing.
TRACE_LLM_ENABLED: bool = _bool_env("TRACE_LLM_ENABLED", default=False)

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "").strip()

# Default matches the problem statement's own sample data (§5.2 PO-7712,
# §5.8 budget tool example: "autonomous purchase threshold of 150000").
# TRACE_APPROVAL_THRESHOLD overrides it for testing; blank keeps the default.
DEFAULT_APPROVAL_THRESHOLD: float = 150_000.0
TRACE_APPROVAL_THRESHOLD: float = _float_env(
    "TRACE_APPROVAL_THRESHOLD", default=DEFAULT_APPROVAL_THRESHOLD
)
