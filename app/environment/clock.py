"""Simulated clock — ARCHITECTURE.md §3, §5.

The environment's notion of "now" is decoupled from wall-clock time so time
pressure (problem statement §4.7; Scenario 6's "12 simulated hours"; §17's
"recheck after 6 simulated hours") is something the team controls, not
something they wait through. Every other module in app/environment reads
`clock.now()` — nothing here calls datetime.utcnow() directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Matches the problem statement's own sample timestamps/dates (2026-09-01
# inventory last_updated, PO-7712 expected_delivery 2026-09-04, PROD-882
# deadline 2026-09-06).
_EPOCH = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


class SimClock:
    """Mutable simulated clock. One process-wide instance (`clock`, below)."""

    def __init__(self, start: datetime = _EPOCH) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: float = 0.0, days: float = 0.0) -> datetime:
        """Move the simulated clock forward. Never backward — a clock that
        can rewind makes staleness/expiry checks (item 4, item 8) unverifiable."""
        if hours < 0 or days < 0:
            raise ValueError("SimClock cannot advance by a negative amount")
        self._now += timedelta(hours=hours, days=days)
        return self._now

    def reset(self, start: datetime = _EPOCH) -> None:
        self._now = start

    def iso(self) -> str:
        return self._now.isoformat()


# Single process-wide simulated clock. Import this, don't instantiate your own.
clock = SimClock()
