"""MONITOR — ARCHITECTURE.md §4 item 2b, §3's first stage. Serves Production
Continuity (35%) and Tool Efficiency (10%).

Deterministic Python. No LLM here, for the same reason as coverage.py: a poll
decision is a comparison between two records, and AGENTS.md rule 1 keeps
comparisons on the deterministic side.

--------------------------------------------------------------------------
What this module decides, and what it does NOT
--------------------------------------------------------------------------
It decides **which purchase orders are worth spending a tracking call on**.
That is the whole job. In particular it does not:

  * compute coverage. `coverage.py` already did. This module consumes
    `CoverageReport` and never re-derives days of coverage, thin-ness, or
    load-bearing-ness from raw inventory. One definition, one owner.
  * decide what "dependable inbound" means. That is explicitly owed to item 3
    (OPEN_ITEMS.md) and is deliberately not defined, narrowed, or shadowed
    here — this module never inspects PO status to judge dependability, it
    only reads the target list coverage.py produced.
  * update supplier reliability. VERIFY (item 3) owns that, on provenance
    grounds. MONITOR reports what tracking said; it does not score anyone for
    it, and it never reads a supplier message.

--------------------------------------------------------------------------
The gate: load-bearing, not thin
--------------------------------------------------------------------------
Signed off in OPEN_ITEMS.md and written into ARCHITECTURE.md §3/§4 item 2b —
not re-derived here. A PO is polled when its withdrawal *alone* would turn some
production order critical.

The distinction is the entire reason this component exists. At Beat 1
(PROJECT.md §4) PROD-882 reads **healthy** at 15.4 days of coverage, so a gate
keyed on thin coverage would not poll PO-7712 until after the delay was already
known — which is precisely the moment §3 says the proactive poll exists to
beat. PROD-882 is healthy *only because* PO-7712 is counted; that contingency
is what makes it worth a call.

`CoverageResult` reports two metrics and the gap between them is the exposure
to supplier claims (§3). This module records that gap on every poll decision as
`exposure_days`: the number of days of coverage that rest on a supplier keeping
their word rather than on stock the plant physically holds. It is the
justification for the call, in the same units as the metric it protects.

--------------------------------------------------------------------------
Why this stays cheap
--------------------------------------------------------------------------
Tool Efficiency is scored on deliberate calls, so every PO gets a recorded
decision — polled or skipped, with the reason — and `MonitorReport` carries the
count against the number of open POs that were available to poll. On the seeded
dataset that is 2 calls out of 21 candidates. The skipped decisions are log
entries, not tool calls; they exist so item 10's count-vs-necessity summary has
something honest to summarise.

This module is stateless and read-only over the environment. It holds no
memory between cycles and never mutates Store, so `build_store()`'s clean-slate
invariant (ARCHITECTURE.md §8) survives any number of poll cycles — which is
what lets the judge panel (Beat 5) reset between injections.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.engine.coverage import (
    CoverageReport,
    DisruptionEvent,
    compute_coverage,
    next_event_id,
)
from app.environment.clock import clock
from app.environment.seed_data import STATE, Store

# Tracking statuses that mean the shipment has not physically moved.
# `label_created_no_pickup` is verbatim from problem statement §5.10, given
# there as the case where tracking contradicts what the supplier asserts.
NOT_MOVED_TRACKING_STATUSES: frozenset[str] = frozenset(
    {"label_created_no_pickup", "not_shipped"}
)

# PO statuses that assert the shipment is already moving. A PO in one of these
# states whose tracking says it never left is the contradiction this poll is
# looking for — the ERP record and the carrier record disagree.
#
# `pending` is deliberately absent: a pending PO is not claiming to have
# shipped, so `not_shipped` tracking agrees with it and is not a finding.
IN_FLIGHT_PO_STATUSES: frozenset[str] = frozenset({"in_transit"})

# Statuses where the PO is still open and could therefore be polled at all.
# This is the denominator for the tool-efficiency count, NOT a dependability
# judgement — dependability is item 3's to define (see module docstring).
OPEN_PO_STATUSES: frozenset[str] = frozenset({"pending", "in_transit", "delayed"})


# ==========================================================================
# Output shapes
# ==========================================================================


class PollDecision(BaseModel):
    """One purchase order, one decision, one reason.

    Recorded for every open PO — including the ones deliberately skipped — so
    the poll count can be defended against the count of calls that were
    *available*. A skipped decision costs a log line, not a tool call.
    """

    po_id: str
    component_id: str
    polled: bool
    reason: str

    # Production orders this PO is load-bearing for: withdraw it and they go
    # critical. Read from CoverageResult.depends_on_po_ids — not recomputed.
    load_bearing_for: list[str] = Field(default_factory=list)

    # days_of_coverage - days_of_coverage_on_hand for the most exposed order
    # above: the days of coverage that rest on a supplier's word rather than on
    # stock in the building. ARCHITECTURE.md §3 calls this the exposure to
    # supplier claims. inf when a component has no consumption.
    exposure_days: float = 0.0

    # Populated only when polled is True — absent values here mean "we did not
    # look", never "there was nothing to see".
    po_status: Optional[str] = None
    tracking_status: Optional[str] = None
    last_movement: Optional[str] = None
    contradicts_po_status: bool = False


class MonitorReport(BaseModel):
    """One MONITOR cycle."""

    polled_at: datetime
    decisions: list[PollDecision]
    events: list[DisruptionEvent]

    # Tool Efficiency (10%): calls spent against calls that were available.
    polls_made: int
    polls_available: int

    def polled(self) -> list[PollDecision]:
        return [d for d in self.decisions if d.polled]

    def skipped(self) -> list[PollDecision]:
        return [d for d in self.decisions if not d.polled]

    def contradictions(self) -> list[PollDecision]:
        """Polls where the carrier record disagreed with the PO record."""
        return [d for d in self.decisions if d.contradicts_po_status]

    def for_po(self, po_id: str) -> Optional[PollDecision]:
        for decision in self.decisions:
            if decision.po_id == po_id:
                return decision
        return None


# ==========================================================================
# Helpers
# ==========================================================================


def _exposure_days(coverage: CoverageReport, po_id: str) -> tuple[list[str], float]:
    """Production orders this PO is load-bearing for, and the largest gap
    between their two coverage metrics.

    Both numbers come straight off `CoverageResult`. Nothing is recomputed —
    coverage.py owns these definitions (see module docstring).
    """
    orders: list[str] = []
    exposure = 0.0
    for result in coverage.results:
        if po_id not in result.depends_on_po_ids:
            continue
        orders.append(result.production_order_id)
        if math.isinf(result.days_of_coverage) or math.isinf(
            result.days_of_coverage_on_hand
        ):
            exposure = math.inf
            continue
        gap = result.days_of_coverage - result.days_of_coverage_on_hand
        exposure = max(exposure, gap)
    return orders, round(exposure, 1) if not math.isinf(exposure) else exposure


def _most_exposed_order(
    coverage: CoverageReport, production_order_ids: list[str]
) -> Optional[str]:
    """The order with the tightest deadline among those given — the one a stall
    on this PO would hurt first."""
    best: Optional[str] = None
    best_days = math.inf
    for production_order_id in production_order_ids:
        result = coverage.for_production_order(production_order_id)
        if result is None:
            continue
        if result.days_to_deadline < best_days:
            best_days = result.days_to_deadline
            best = production_order_id
    return best


# ==========================================================================
# The cycle
# ==========================================================================


def run_monitor_cycle(
    store: Optional[Store] = None,
    *,
    coverage: Optional[CoverageReport] = None,
    now: Optional[datetime] = None,
) -> MonitorReport:
    """Poll tracking for every load-bearing PO. Read-only and repeatable.

    Pass an existing `coverage` report to reuse a pass the orchestrator has
    already run — recomputing it here would be exactly the kind of unnecessary
    work Tool Efficiency penalises. Omit it and one is computed.

    Emits a `supplier_delay` DisruptionEvent (source `proactive_poll`) when the
    carrier record contradicts the PO record. It does not touch reliability
    scores: that is VERIFY's call, on provenance grounds (AGENTS.md rule 4).
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now
    coverage = compute_coverage(store, now=now) if coverage is None else coverage

    # The gate. Signed off in OPEN_ITEMS.md — read, never re-derived.
    targets = coverage.polling_targets()

    decisions: list[PollDecision] = []
    events: list[DisruptionEvent] = []
    polls_available = 0

    for po in sorted(store.list_purchase_orders(), key=lambda p: p.po_id):
        if po.status not in OPEN_PO_STATUSES:
            # Closed POs are not pollable at all, so they are not part of the
            # tool-efficiency denominator either — counting them would flatter
            # the ratio for free.
            continue
        polls_available += 1

        load_bearing_for, exposure = _exposure_days(coverage, po.po_id)

        if po.po_id not in targets:
            decisions.append(
                PollDecision(
                    po_id=po.po_id,
                    component_id=po.component_id,
                    polled=False,
                    reason="not_load_bearing",
                    load_bearing_for=load_bearing_for,
                    exposure_days=exposure,
                )
            )
            continue

        # --- the actual tool call: GET /tracking/{po_id} (§5.10, §6) --------
        tracking = store.get_tracking(po.po_id)
        if tracking is None:
            decisions.append(
                PollDecision(
                    po_id=po.po_id,
                    component_id=po.component_id,
                    polled=True,
                    reason="load_bearing_no_tracking_record",
                    load_bearing_for=load_bearing_for,
                    exposure_days=exposure,
                    po_status=po.status,
                )
            )
            continue

        contradicts = (
            po.status in IN_FLIGHT_PO_STATUSES
            and tracking.tracking_status in NOT_MOVED_TRACKING_STATUSES
        )

        decisions.append(
            PollDecision(
                po_id=po.po_id,
                component_id=po.component_id,
                polled=True,
                reason="load_bearing",
                load_bearing_for=load_bearing_for,
                exposure_days=exposure,
                po_status=po.status,
                tracking_status=tracking.tracking_status,
                last_movement=tracking.last_movement,
                contradicts_po_status=contradicts,
            )
        )

        if not contradicts:
            continue

        production_order_id = _most_exposed_order(coverage, load_bearing_for)
        summary = (
            f"{po.po_id} is recorded as {po.status}, but tracking reads "
            f"{tracking.tracking_status}. The shipment has not moved. "
            f"{exposure} days of coverage"
            + (
                f" on {production_order_id}"
                if production_order_id is not None
                else ""
            )
            + " rest on this delivery arriving as booked."
        )
        events.append(
            DisruptionEvent(
                event_id=next_event_id(),
                type="supplier_delay",
                component_id=po.component_id,
                po_id=po.po_id,
                detected_at=now,
                source="proactive_poll",
                production_order_id=production_order_id,
                detail={
                    # ARCHITECTURE.md §7 writes `detail` as free text; the
                    # shipped model (coverage.py) carries a dict. `summary` is
                    # that free text, kept alongside the figures rather than
                    # instead of them. See OPEN_ITEMS.md.
                    "summary": summary,
                    "po_status": po.status,
                    "tracking_status": tracking.tracking_status,
                    "last_movement": tracking.last_movement,
                    "exposure_days": exposure,
                    "load_bearing_for": load_bearing_for,
                    "detected_by": "proactive_poll_before_supplier_message",
                },
            )
        )

    return MonitorReport(
        polled_at=now,
        decisions=decisions,
        events=events,
        polls_made=sum(1 for d in decisions if d.polled),
        polls_available=polls_available,
    )
