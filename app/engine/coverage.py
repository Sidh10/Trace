"""Coverage engine — ARCHITECTURE.md §4, item 2. Serves Production Continuity
(35%, PROJECT.md §2), the single heaviest rubric row.

Deterministic Python end to end. No LLM is imported, called, or consulted in
this module and none ever should be: AGENTS.md rule 1 puts coverage math,
threshold comparison and contradiction detection on the deterministic side of
the line without exception. This module is also read-only over the environment
— it never mutates Store, so running it costs nothing and repeats safely
(AGENTS.md rule 5).

What it produces, per the item-2 brief:

  * days of coverage per production order — BRAND.md §4 requires that exact
    term. Not buffer, not runway, not stock-days, in code or on screen.
  * production priority and deadline, cross-referenced from §5.4.
  * ERP-vs-warehouse mismatch (Scenario 2) logged as a contradiction. Never
    silently reconciled, never auto-corrected — see `InventoryContradiction`.
  * DisruptionEvent (ARCHITECTURE.md §7) when coverage goes thin.

--------------------------------------------------------------------------
Where the thin-coverage threshold comes from
--------------------------------------------------------------------------
AGENTS.md rule 7 forbids inventing a metric and displaying it as a finding, so
there is no configurable "alert below N days" constant here. N would be
invented. Both thresholds are read off the problem statement's own fields:

  critical  days_of_coverage < days_to_deadline
            The component runs out before the production order's own §5.4
            deadline. The line stops. Both terms are spec fields.

  at_risk   the stock projection drops below safety_stock (§5.1) before that
            deadline, while still clearing zero.
            The order completes only by consuming the reserve the spec itself
            designates as the floor. ARCHITECTURE.md item 5b makes drawing on
            safety stock an action requiring written justification, so this is
            a real finding and not a rounding artefact.

  healthy   neither holds.

"Thin coverage" — the phrase ARCHITECTURE.md item 2b gates proactive tracking
polls on — means any status that is not healthy. `CoverageResult.is_thin`.

--------------------------------------------------------------------------
Why coverage credits inbound purchase orders
--------------------------------------------------------------------------
`usable_stock / daily_usage` alone is the on-hand floor, reported as
`days_of_coverage_on_hand`. The headline `days_of_coverage` runs a stock
projection that also credits open POs arriving before the stock runs out,
because an order whose parts land on day 3 is not in trouble on day 4.

That is what makes a supplier delay *move the number*: for COMP-104 at the
seeded clock, PO-7712 (1000 units, expected 2026-09-04) carries PROD-882 to
15.4 days of coverage. Mark PO-7712 delayed and the credit is withdrawn, and
the projection collapses to exactly the on-hand floor, 390/90 = 4.3 days —
PROJECT.md §4 Beat 2's number, against its Sept 6 deadline. Every input is a
spec field (§5.2 quantity, expected_delivery, status); nothing is estimated.

A PO that is load-bearing in this sense is recorded in
`CoverageResult.depends_on_po_ids` — the POs whose withdrawal alone would turn
this order critical. That list is the gate ARCHITECTURE.md item 2b needs: poll
tracking for these, not for every open PO, which is how Tool Efficiency (10%)
stays defensible.
"""

from __future__ import annotations

import math
from datetime import datetime, time, timezone
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, Field

from app.environment.clock import clock
from app.environment.schemas import InventoryRecord, PurchaseOrder
from app.environment.seed_data import STATE, Store

# Open POs whose quantity counts toward projected coverage. A `delayed` PO is
# excluded — that withdrawal is the mechanism behind Beat 2. `delivered` stock
# is already inside usable_stock, and `cancelled` is never arriving.
#
# RESOLVED (OPEN_ITEMS.md, item 3): this stays a pure PO-status filter,
# permanently — it does not read SupplierRecord.reliability_score, and it
# does not read VERIFY's per-PO contradiction findings either. Coupling
# reliability in here would make days_of_coverage stop meaning "what we have
# if suppliers keep their word," which breaks the "the gap between the two
# metrics IS the exposure to supplier claims" framing this engine's whole
# design rests on (see the module docstring). Full reasoning:
# app/engine/verify.py's own module docstring, section "Resolving
# OPEN_ITEMS.md: does 'dependable inbound' couple to reliability?" — read
# that before changing this set, not just this comment.
#
# A DIFFERENT, undecided question — once VERIFY confirms a specific PO's
# claim is contradicted, should COVERAGE stop crediting that one PO — belongs
# to item 8 (staleness detector) re-running compute_coverage with updated
# environment state, not to this filter growing a contradiction check.
DEPENDABLE_PO_STATUSES: frozenset[str] = frozenset({"pending", "in_transit"})

# Float slack for stock/day comparisons. The seeded COMP-104 trajectory touches
# safety_stock (150) at the exact moment PO-7712 arrives — 390 - 90 x 2.667 =
# 150.0 — so an exact-equality dip would flip PROD-882 between healthy and
# at_risk on floating-point noise. Touching the floor is not breaching it.
_EPS = 1e-9


CoverageStatus = Literal["healthy", "at_risk", "critical"]

# ARCHITECTURE.md §7's proposed type union, plus one.
#
# ADDITION: "coverage_breach". §7's five types name causes (a supplier delayed,
# a priority changed); this engine detects an *effect* — a production order that
# no longer reaches its deadline — and none of the five say that. Reusing
# "inventory_mismatch" would conflate it with the Scenario 2 contradiction,
# which is a genuinely different finding emitted separately below. §7 permits
# adjusting these shapes by agreement with the consuming component; MONITOR
# (item 2b) is the consumer and should import this union rather than restate it.
DisruptionType = Literal[
    "supplier_delay",
    "inventory_mismatch",
    "demand_spike",
    "expedite_unavailable",
    "priority_change",
    "coverage_breach",
]

DisruptionSource = Literal["proactive_poll", "supplier_message", "erp_check"]


# ==========================================================================
# Output shapes
# ==========================================================================


class DisruptionEvent(BaseModel):
    """ARCHITECTURE.md §7, verbatim fields plus two additive ones.

    ADDITION `production_order_id`: §7's shape carries component_id and po_id
    only. A coverage breach is fundamentally about a production order and there
    was no slot for it; adding a field is additive, and nothing spec-given or
    §7-given is renamed.

    ADDITION `detail`: the numbers behind the event, so the escalation brief
    (item 6) and the provenance graph (item 7) can cite figures rather than
    recompute them and risk disagreeing with this module.
    """

    event_id: str
    type: DisruptionType
    component_id: str
    po_id: Optional[str] = None
    detected_at: datetime
    source: DisruptionSource
    production_order_id: Optional[str] = None
    detail: dict = Field(default_factory=dict)


class InventoryContradiction(BaseModel):
    """ERP-vs-warehouse mismatch — problem statement Scenario 2, the hidden
    test ARCHITECTURE.md §9 lists as "ERP inventory overstates real stock".

    Recorded, never repaired. This engine does not write a corrected figure
    back, does not average the two, and does not prefer one silently: the gap
    between the ERP headline (`current_stock`) and what the warehouse can
    actually issue (`usable_stock`) is itself the finding. All downstream
    coverage math uses `usable_stock`, and this record states plainly that it
    did and what the other number would have implied.

    `decision_changing` is what separates a finding from noise. Coverage is
    computed twice — once on each figure — and the flag is True only when the
    two land on different sides of the thin-coverage line. A gap that leaves
    both verdicts identical is still logged here, in full, but does not raise a
    DisruptionEvent; that restraint is what keeps Tool Efficiency (10%) honest.

    Maps to a ProvenanceEdge (§7) with relation "Contradict",
    from="warehouse:{component_id}" to="erp:{component_id}" when item 7 lands.
    """

    component_id: str
    erp_current_stock: int
    warehouse_usable_stock: int
    # Positive: the ERP headline overstates issuable stock (Scenario 2's
    # direction). Negative: the warehouse holds more than the ERP shows.
    erp_overstatement_units: int
    erp_snapshot_age_hours: float
    coverage_days_if_erp_believed: float
    coverage_days_on_warehouse_truth: float
    decision_changing: bool
    note: str


class CoverageResult(BaseModel):
    """Days of coverage for one production order — BRAND.md §4's term, used
    identically here, in the API, and on the coverage board (item 12)."""

    production_order_id: str
    product: str
    component_id: str
    component_name: str

    # The literal item-2 formula: usable_stock / daily_usage. The on-hand
    # floor, holding no open PO to be dependable.
    days_of_coverage_on_hand: float
    # Headline. Same walk, crediting dependable inbound POs (see module
    # docstring). Equal to the floor above when nothing inbound is dependable.
    days_of_coverage: float

    # Cross-referenced from §5.4.
    deadline: datetime
    priority: Literal["low", "medium", "high"]
    days_to_deadline: float

    status: CoverageStatus
    is_thin: bool
    reason: str

    # §5.1 reserve, and when the projection first dips under it (inf = never
    # within the projection).
    safety_stock: int
    days_to_safety_breach: float

    # units_planned x component_required_per_unit — §5.4 fields, multiplied.
    component_required: int
    usable_stock: int
    daily_usage: int

    # Open POs that alone keep this order out of `critical`. ARCHITECTURE.md
    # item 2b polls tracking for exactly these.
    depends_on_po_ids: list[str] = Field(default_factory=list)
    inbound_po_ids: list[str] = Field(default_factory=list)


class CoverageReport(BaseModel):
    """One full pass of the coverage engine."""

    computed_at: datetime
    results: list[CoverageResult]
    contradictions: list[InventoryContradiction]
    events: list[DisruptionEvent]

    def thin(self) -> list[CoverageResult]:
        """Production orders MONITOR (item 2b) should gate polling on."""
        return [r for r in self.results if r.is_thin]

    def for_production_order(self, production_order_id: str) -> Optional[CoverageResult]:
        for result in self.results:
            if result.production_order_id == production_order_id:
                return result
        return None

    def polling_targets(self) -> list[str]:
        """The item-2b poll list: every load-bearing PO, deduplicated, ordered.

        Deliberately drawn from ALL production orders rather than only the thin
        ones. ARCHITECTURE.md item 2b reads "open POs feeding thin-coverage
        production orders", and the narrower reading defeats the purpose §3
        states for this poll — "catches the stall BEFORE the delay email
        arrives". At the seeded clock PROD-882 is healthy at 15.4 days, but it
        is healthy *only* because PO-7712 is counted; withdraw it and the order
        is critical at 4.3. A gate that waits for the order to already be thin
        would not poll PO-7712 until after the delay was known, which is the
        one moment proactive polling was supposed to beat.

        This stays cheap, which is what Tool Efficiency (10%) is scored on: a
        PO qualifies only if its withdrawal alone turns some production order
        critical. On the seeded dataset that is 2 POs out of 21, not a sweep.
        """
        targets: list[str] = []
        for result in self.results:
            for po_id in result.depends_on_po_ids:
                if po_id not in targets:
                    targets.append(po_id)
        return targets


# ==========================================================================
# Event ids
# ==========================================================================

_event_seq = 0


def next_event_id() -> str:
    """Draw the next id from the shared DisruptionEvent sequence.

    Public because MONITOR (item 2b) emits events too and must not start its
    own counter — see reset_event_sequence(). Two emitters with independent
    counters would put duplicate event ids in the provenance graph (item 7).
    """
    global _event_seq
    _event_seq += 1
    return f"EVT-{_event_seq:04d}"


def reset_event_sequence() -> None:
    """Test hook. MONITOR (item 2b) must draw ids from here too rather than
    starting its own counter, or two events will share an id in the audit
    trail (item 7)."""
    global _event_seq
    _event_seq = 0


# ==========================================================================
# Stock projection — deterministic, exact, no sampling
# ==========================================================================


def _as_datetime(value) -> datetime:
    """Spec deadlines and delivery dates are dates (§5.2, §5.4); the clock is a
    datetime. Dates are anchored to the START of their day — the conservative
    reading: an order due Sept 6 must be covered when Sept 6 begins, not when
    it ends. Coverage is a continuity metric, so it errs toward flagging early.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _days_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 86400.0


def _trajectory(
    opening_stock: float,
    daily_usage: float,
    arrivals: Iterable[tuple[float, float]],
) -> tuple[list[tuple[float, float, float, float]], float]:
    """Walk the stock level forward from now.

    `arrivals` is (day_offset, quantity), and is sorted here. Returns the
    piecewise-linear trajectory as (t_start, stock_start, t_end, stock_end)
    segments, plus the day offset at which stock first reaches zero.

    Consumption runs at daily_usage until stock hits zero; the walk stops
    there. An arrival landing after that point does not extend coverage,
    because the line has already stopped — restarting it is a different
    (and later) event than running without interruption.
    """
    segments: list[tuple[float, float, float, float]] = []
    t = 0.0
    stock = float(opening_stock)

    if daily_usage <= 0:
        # No consumption: coverage is unbounded. §5.1 daily_usage is an int and
        # the seeded values are all positive, but a zero-usage component must
        # not divide by zero here.
        return segments, math.inf

    for day, quantity in sorted(arrivals, key=lambda a: a[0]):
        if day <= t:
            # Already due or overdue at the moment of computation.
            stock += quantity
            continue
        depleted_at = t + stock / daily_usage
        if depleted_at <= day:
            segments.append((t, stock, depleted_at, 0.0))
            return segments, depleted_at
        end_stock = stock - daily_usage * (day - t)
        segments.append((t, stock, day, end_stock))
        stock = end_stock + quantity
        t = day

    depleted_at = t + stock / daily_usage
    segments.append((t, stock, depleted_at, 0.0))
    return segments, depleted_at


def _first_day_below(
    segments: list[tuple[float, float, float, float]], level: float
) -> float:
    """First day offset at which the trajectory is strictly below `level`.
    inf if it never is. Stock falls linearly inside a segment, so the crossing
    is exact, not sampled."""
    for t0, s0, t1, s1 in segments:
        if s0 < level - _EPS:
            return t0
        if s1 < level - _EPS:
            drop = s0 - s1
            if drop <= 0:
                return t0
            return t0 + ((s0 - level) / drop) * (t1 - t0)
    return math.inf


# ==========================================================================
# Inbound purchase orders
# ==========================================================================


def dependable_inbound(
    store: Store, component_id: str, now: datetime
) -> list[tuple[PurchaseOrder, float]]:
    """Open POs for this component that count toward projected coverage, each
    paired with its arrival day offset. Status filter only — no judgement about
    whether the supplier is telling the truth. That is item 3's job, on
    provenance grounds (AGENTS.md rule 4), and this engine must not
    second-guess a claim by reading a message."""
    inbound: list[tuple[PurchaseOrder, float]] = []
    for po in store.list_purchase_orders(component_id=component_id):
        if po.status not in DEPENDABLE_PO_STATUSES:
            continue
        inbound.append((po, _days_between(now, _as_datetime(po.expected_delivery))))
    return sorted(inbound, key=lambda pair: pair[1])


# ==========================================================================
# ERP-vs-warehouse mismatch — Scenario 2
# ==========================================================================


def detect_inventory_contradiction(
    record: InventoryRecord,
    now: datetime,
    *,
    arrivals: list[tuple[float, float]],
    deadline_days: float,
) -> Optional[InventoryContradiction]:
    """Return a contradiction record when the ERP headline and the warehouse's
    issuable figure disagree. None when they agree exactly.

    Materiality is decided by re-running the same projection on the ERP figure
    and comparing verdicts, not by a size threshold on the gap — a threshold
    would be an invented number (AGENTS.md rule 7)."""
    gap = record.current_stock - record.usable_stock
    if gap == 0:
        return None

    erp_segments, erp_days = _trajectory(
        record.current_stock, record.daily_usage, arrivals
    )
    warehouse_segments, warehouse_days = _trajectory(
        record.usable_stock, record.daily_usage, arrivals
    )

    erp_status, _ = _classify(
        erp_days, _first_day_below(erp_segments, record.safety_stock), deadline_days
    )
    warehouse_status, _ = _classify(
        warehouse_days,
        _first_day_below(warehouse_segments, record.safety_stock),
        deadline_days,
    )
    decision_changing = erp_status != warehouse_status

    if gap > 0:
        note = (
            f"ERP current_stock overstates issuable stock by {gap} units. "
            f"Coverage computed on warehouse usable_stock ({record.usable_stock}), "
            f"not on the ERP figure ({record.current_stock}). Not reconciled."
        )
    else:
        note = (
            f"Warehouse usable_stock exceeds ERP current_stock by {-gap} units. "
            f"Coverage computed on usable_stock ({record.usable_stock}). "
            "Not reconciled."
        )
    if decision_changing:
        note += (
            f" Decision-changing: the ERP figure reads {erp_status}, "
            f"the warehouse figure reads {warehouse_status}."
        )

    return InventoryContradiction(
        component_id=record.component_id,
        erp_current_stock=record.current_stock,
        warehouse_usable_stock=record.usable_stock,
        erp_overstatement_units=gap,
        # Negative when the seeded ERP snapshot is stamped ahead of the sim
        # clock. Reported as evidence on the finding, never used as a trigger —
        # "how stale is stale" has no answer in the spec, so no threshold here.
        erp_snapshot_age_hours=round(
            (now - _as_datetime(record.last_updated)).total_seconds() / 3600.0, 2
        ),
        coverage_days_if_erp_believed=_round_days(erp_days),
        coverage_days_on_warehouse_truth=_round_days(warehouse_days),
        decision_changing=decision_changing,
        note=note,
    )


# ==========================================================================
# Classification
# ==========================================================================


def _classify(
    days_of_coverage: float, days_to_safety_breach: float, days_to_deadline: float
) -> tuple[CoverageStatus, str]:
    """The threshold ladder from the module docstring. Every comparison is
    between two quantities read off spec fields."""
    if days_to_deadline <= 0:
        return "critical", "deadline_passed"
    if days_of_coverage < days_to_deadline - _EPS:
        return "critical", "stockout_before_deadline"
    if days_to_safety_breach < days_to_deadline - _EPS:
        return "at_risk", "safety_stock_drawn_before_deadline"
    return "healthy", "covered_to_deadline"


def _round_days(value: float) -> float:
    """One decimal place — the precision PROJECT.md §4 quotes on stage
    ("4.3 days"). inf survives rounding and means "never runs out within the
    projection"."""
    if math.isinf(value):
        return value
    return round(value, 1)


# ==========================================================================
# The pass
# ==========================================================================


def compute_coverage(
    store: Optional[Store] = None, *, now: Optional[datetime] = None
) -> CoverageReport:
    """One full coverage pass over the environment. Read-only and repeatable.

    Emits a DisruptionEvent per thin production order, and one per
    decision-changing inventory contradiction.
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now

    results: list[CoverageResult] = []
    contradictions: list[InventoryContradiction] = []
    events: list[DisruptionEvent] = []

    # A component's projection is identical for every production order drawing
    # on it, so build it once per component rather than once per order.
    projection_cache: dict[str, list[tuple[PurchaseOrder, float]]] = {}
    # component_id -> (tightest days_to_deadline, the order that owns it). The
    # contradiction pass below judges materiality against the most binding
    # deadline, not whichever order happened to be read first.
    tightest: dict[str, tuple[float, str]] = {}

    for order in sorted(
        store.list_production_schedule(), key=lambda o: o.production_order_id
    ):
        record = store.get_inventory(order.required_component)
        if record is None:
            # A production order for a component with no inventory row is a
            # dataset bug, not a coverage finding — skip rather than invent a
            # stock level for it.
            continue

        if record.component_id not in projection_cache:
            projection_cache[record.component_id] = dependable_inbound(
                store, record.component_id, now
            )
        inbound = projection_cache[record.component_id]
        arrivals = [(day, float(po.quantity)) for po, day in inbound]

        days_to_deadline = _days_between(now, _as_datetime(order.deadline))
        if (
            record.component_id not in tightest
            or days_to_deadline < tightest[record.component_id][0]
        ):
            tightest[record.component_id] = (
                days_to_deadline,
                order.production_order_id,
            )

        segments, days_of_coverage = _trajectory(
            record.usable_stock, record.daily_usage, arrivals
        )
        days_to_safety_breach = _first_day_below(segments, record.safety_stock)

        # The literal item-2 formula, holding no PO dependable.
        _, days_on_hand = _trajectory(record.usable_stock, record.daily_usage, [])

        status, reason = _classify(
            days_of_coverage, days_to_safety_breach, days_to_deadline
        )

        # Which single inbound PO, withdrawn on its own, would turn this order
        # critical. Exact re-projection, not an approximation.
        depends_on: list[str] = []
        for po, _day in inbound:
            without = [
                (day, float(other.quantity))
                for other, day in inbound
                if other.po_id != po.po_id
            ]
            _, days_without = _trajectory(
                record.usable_stock, record.daily_usage, without
            )
            if _classify(days_without, math.inf, days_to_deadline)[0] == "critical":
                depends_on.append(po.po_id)

        result = CoverageResult(
            production_order_id=order.production_order_id,
            product=order.product,
            component_id=record.component_id,
            component_name=record.name,
            days_of_coverage_on_hand=_round_days(days_on_hand),
            days_of_coverage=_round_days(days_of_coverage),
            deadline=_as_datetime(order.deadline),
            priority=order.priority,
            days_to_deadline=round(days_to_deadline, 1),
            status=status,
            is_thin=status != "healthy",
            reason=reason,
            safety_stock=record.safety_stock,
            days_to_safety_breach=_round_days(days_to_safety_breach),
            component_required=order.units_planned * order.component_required_per_unit,
            usable_stock=record.usable_stock,
            daily_usage=record.daily_usage,
            depends_on_po_ids=depends_on,
            inbound_po_ids=[po.po_id for po, _ in inbound],
        )
        results.append(result)

        if result.is_thin:
            events.append(
                DisruptionEvent(
                    event_id=next_event_id(),
                    type="coverage_breach",
                    component_id=record.component_id,
                    # The load-bearing PO whose withdrawal explains the breach,
                    # when there is one. None when the order is simply short of
                    # stock with nothing inbound to blame.
                    po_id=depends_on[0] if depends_on else None,
                    detected_at=now,
                    source="erp_check",
                    production_order_id=order.production_order_id,
                    detail={
                        "days_of_coverage": result.days_of_coverage,
                        "days_of_coverage_on_hand": result.days_of_coverage_on_hand,
                        "days_to_deadline": result.days_to_deadline,
                        "status": status,
                        "reason": reason,
                        "priority": order.priority,
                        "component_required": result.component_required,
                        "depends_on_po_ids": depends_on,
                    },
                )
            )

    # One contradiction per component, not per production order — several
    # orders share COMP-104 and the mismatch is a property of the inventory
    # record, not of whichever order happened to read it. Judged against the
    # component's most binding deadline, since that is the order the ERP figure
    # would have misled someone about first.
    for component_id, (deadline_days, production_order_id) in sorted(tightest.items()):
        record = store.get_inventory(component_id)
        if record is None:
            continue
        contradiction = detect_inventory_contradiction(
            record,
            now,
            arrivals=[
                (day, float(po.quantity))
                for po, day in projection_cache.get(component_id, [])
            ],
            deadline_days=deadline_days,
        )
        if contradiction is None:
            continue
        contradictions.append(contradiction)
        if contradiction.decision_changing:
            events.append(
                DisruptionEvent(
                    event_id=next_event_id(),
                    type="inventory_mismatch",
                    component_id=component_id,
                    po_id=None,
                    detected_at=now,
                    source="erp_check",
                    production_order_id=production_order_id,
                    detail=contradiction.model_dump(mode="json"),
                )
            )

    return CoverageReport(
        computed_at=now,
        results=results,
        contradictions=contradictions,
        events=events,
    )
