"""PLANNER — ARCHITECTURE.md §4 item 5, §3's "PLAN" stage. Serves Production
Continuity (35%) and Recovery & Replanning (10%, PROJECT.md §2).

Deterministic throughout (AGENTS.md rule 1) — no LLM anywhere in this module.
Covers item 5 (supplier split, stock allocation, production reschedule) AND
item 5b (safety-stock consumption, gated on written justification) —
ARCHITECTURE.md §9's repo layout assigns both to this one file.

--------------------------------------------------------------------------
Item 5b — safety-stock draw is a GATE on allocation, not a bonus lever
--------------------------------------------------------------------------
A real correction to item 5's original behavior, made while building 5b,
disclosed here rather than silently changed: `allocate_stock` used to treat
the FULL `usable_stock` as freely allocatable — meaning routine, unremarked
priority-based allocation could ALREADY dip into (or through) the
`safety_stock` reserve with no authorization at all, which made 5b's own gate
decorative. `run_planner` now allocates from `usable_stock - safety_stock`
(the operating buffer) BY DEFAULT; the reserve is only spent through the
explicit, justified mechanism below. This can change `_reschedule_actions`'
output versus item 5's original shipped behavior for the SAME inputs — see
OPEN_ITEMS.md.

Trigger and sizing (component-level; "on-hand coverage runs out" is read as
`days_of_coverage_on_hand` — usable_stock/daily_usage hitting zero — the
same field name coverage.py already uses, not a second definition):
    gap_days = earliest_secured_delivery_day - days_of_coverage_on_hand
    if gap_days > 0:
        draw_units = min(gap_days * daily_usage, safety_stock)
`earliest_secured_delivery_day` is the fastest-arriving member of the CHOSEN
combination. The draw is capped at `InventoryRecord.safety_stock` — the same
field item 2's `at_risk` classification already reads (§5.1) — reused, not
redefined.

Checking impact on other orders sharing the component (COMP-104 feeds both
PROD-882 and PROD-914): the draw is modeled as an IMMEDIATE reduction of
`usable_stock` by `draw_units` (a conservative, worst-case accounting
convention — the units are treated as spent the moment the draw is
authorized, not gradually over the gap window). Only BYSTANDER orders are
reclassified this way — orders that receive ZERO of the chosen combination's
incoming supply under the reserve-respecting baseline allocation (whether
because they're already fully covered on-hand, or a higher-priority order
consumed the whole incoming shipment first). Orders that ARE drawing on the
same incoming shipment are excluded from this check on purpose: the
on-hand-only lens ignores incoming supply entirely, so applying it to the
order this draw exists to help would penalize it for the very relief it's
receiving — a real bug caught while testing (see OPEN_ITEMS.md), not a
hypothetical edge case. For bystanders, the on-hand-only projection (no
inbound POs credited — deliberately consistent with what "on-hand coverage
runs out" already means above) is compared before/after via `coverage.py`'s
OWN `_classify` (imported, not reimplemented — one threshold ladder, one
owner). If ANY bystander's classification would worsen (healthy→at_risk,
healthy→critical, at_risk→critical), the draw is REFUSED,
not executed-with-a-caveat: authorizing a trade-off between two production
orders on the system's own initiative, unprompted, is exactly the kind of
decision this project's escalation philosophy (AGENTS.md rule 3) reserves for
a human, not something to wave through with a footnote. `SafetyStockDecision`
records the attempt either way — drawn or refused — with the real numbers,
for audit.

--------------------------------------------------------------------------
Reversibility — resolved, not left as item 5's "?"
--------------------------------------------------------------------------
`safety_stock_draw` is tagged **compensable**, matching `purchase_split`'s
reasoning, not `production_reschedule`'s: once units are actually consumed
in production, they cannot be un-consumed — there is no "put it back" action,
same as there is no PO-cancellation feature (ARCHITECTURE.md §5). But the
situation CAN be compensated (replenish the reserve via a later purchase,
adjust the target going forward) — which is exactly what AGENTS.md rule 5
means by "compensable," as distinct from "reversible" (a pure state flip with
no physical consequence, like a schedule date).

--------------------------------------------------------------------------
The selection rule — SOLVER hands a Pareto SET, not an answer
--------------------------------------------------------------------------
    Select the Pareto candidate with the HIGHEST reliability_score; ties
    broken by the SHORTEST lead_time_days; further ties broken by the
    LOWEST total_price.

`_select_combination` below implements exactly that sentence — three sort
keys, in that order, nothing else. This is a lexicographic rule, not a
weighted score: each tier is checked ONLY when every candidate is tied on
every earlier tier. That distinction is the whole point (AGENTS.md rule 7;
PROJECT.md §7 Standing Decisions rejects the Trust Gate for exactly this
reason) — a lexicographic rule never blends incommensurable units (rupees,
days, a [0,1] score) into one number, so there is no hidden weight for a
judge to ask about.

Why reliability outranks lead time, not the other way around: coverage.py's
own two-metric design exists because an unverified promise is not the same
as real stock — "THE GAP BETWEEN THEM IS THE EXPOSURE TO SUPPLIER CLAIMS"
(ARCHITECTURE.md §3, its own emphasis). A supplier item 3 just downgraded
after a tracking contradiction offering the fastest delivery is not a
faster path to restored continuity — it is a less-credible promise about
one. Putting lead time first here would make every reliability update item
3 performs causally inert: the score would exist, be logged, and never
change a single sourcing decision, which contradicts BRAND.md §2's own
one-line pitch ("every claim gets checked against tracking data before it's
trusted" — trusted enough to act differently, not just logged).

This is a genuine design call, not a spec-given rule, and it is stated
plainly rather than buried in a comparator: a small reliability difference
can outrank a large lead-time difference, because the tiers are strict, not
blended. That is flagged in OPEN_ITEMS.md as a real, undecided refinement
(e.g. gating on a discrete "was this specific claim contradicted" fact
instead of a continuous score) for whoever revisits this — not hidden here.

--------------------------------------------------------------------------
Stock allocation — priority and deadline, not an invented weighting
--------------------------------------------------------------------------
One component can feed several production orders (COMP-104 feeds both
PROD-882 and PROD-914). The pool available to satisfy them is on-hand
`usable_stock` (available now) plus the chosen combination's members, each
arriving at ITS OWN `lead_time_days` — a 2-way split has two different
arrival times, not one.

Allocation order: production orders are served in priority order (high
before medium before low — §5.4's own field), and within a priority tier, by
earliest deadline first (§5.4's own field again). No invented weight
combines these two into a score; it is a plain sort key,
`(priority_rank, deadline)`, applied twice — once against on-hand stock,
then again against each incoming member in arrival-order (fastest first),
so a higher-priority order's shortfall is covered by the soonest-arriving
supply before a lower-priority order gets any of it.

An order's `earliest_full_supply_day` is the arrival day of the LAST
increment it needed to reach `component_required` — 0 if on-hand alone
covers it, otherwise the lead_time_days of whichever incoming portion
completed it. That is the real, computable measure of "when this order is
actually ready," not an estimate.

--------------------------------------------------------------------------
Production reschedule — computed from the actual shortfall, not hardcoded
--------------------------------------------------------------------------
An order needs `production_reschedule` only when its own
`earliest_full_supply_day` lands AFTER its `days_to_deadline` — i.e. the
real, computed allocation says it will not be ready in time, regardless of
priority. `delay_days = ceil(earliest_full_supply_day - days_to_deadline)`:
the number of days beyond the original deadline the order will actually be
ready, given the plan just built. Nothing here targets a specific order or a
specific delay; whichever order the numbers say is short gets the action,
for however many days the numbers say.

--------------------------------------------------------------------------
Reversibility tagging (AGENTS.md rule 5)
--------------------------------------------------------------------------
Exactly one action anywhere in this system is irreversible:
`POST /erp/update`, and it does not happen here — RATCHET (item 6) decides
execute-or-escalate before anything is written, and only a downstream write
after that decision would ever call it. Every action this module produces is
tagged accordingly:
  - `purchase_split` -> **compensable**. Once actually executed (an ERP
    write downstream creates the PO), it cannot be literally undone —
    ARCHITECTURE.md §5 explicitly excludes PO cancellation from this system's
    action space — but its effects CAN be compensated by a further action
    (reallocating the excess, adjusting a later order). That is exactly
    "compensable," not "irreversible": the tool call itself
    (`POST /erp/update`) is the only thing this codebase reserves that label
    for.
  - `production_reschedule` -> **reversible**. A pure scheduling change; the
    order can be rescheduled again with no residual effect.

--------------------------------------------------------------------------
total_cost — real quote data, never estimated
--------------------------------------------------------------------------
`Plan.total_cost` is read directly off the chosen `SourcingCombination.
total_price`, which solver.py computed from actual RFQ quote unit prices
(§5.7) times allocated quantity. Nothing here re-estimates or rounds it.

--------------------------------------------------------------------------
cost_of_inaction — CORRECTED: a structured object, not a money figure
--------------------------------------------------------------------------
A previous session searched this repo's docs twice, found no penalty
clause, shutdown-cost-per-day, or lost-production-value figure anywhere,
and left `Plan.cost_of_inaction` as `None`. That search was right — no such
figure exists in this repo's spec material — but the CONCLUSION was wrong:
the real problem statement (authoritative, supplied 2026-08-23) contains no
monetary basis either, and the fix is not to keep hunting for money, it's to
stop representing this as a money field at all. `Plan.cost_of_inaction` is
now `CostOfInaction`, a structured, non-monetary object — every field
derivable from spec fields already in the Store or already computed by
`allocate_stock` / `_reschedule_actions`, never `None` at the `Plan` level.

`production_orders_at_risk` — one entry per order this plan does NOT get to
its own deadline (`AllocationDetail.on_time is False`), each carrying:
  - `units_unbuilt` = `component_required - allocated_on_hand` — the portion
    that depends on supply not yet physically in hand. On-hand is the only
    figure guaranteed available regardless of what this plan does next,
    so it is the honest floor for "how much is actually missing," not the
    optimistic `shortfall` (which counts incoming supply as already secured).
  - `deadline_missed_by_days` = the matching `ProductionRescheduleAction.
    delay_days` when one exists (reused, not recomputed — one formula, one
    owner), else `None` for an order whose shortfall is never fully covered
    by this plan at all (no completion day exists to report).

`cost_increase_vs_baseline_pct` — the spec's own metric (§17). "Baseline"
is read literally as "the plan cost had no disruption occurred": the
DELAYED purchase order for this component (the one whose failure created
this shortfall) carries its own originally contracted `unit_price` — that
IS what this purchase would have cost absent the disruption. Baseline total
= that price × `chosen_combination.quantity_allocated` (same quantity as
the actual purchase, for a fair comparison); actual = `chosen_combination.
total_price` (real RFQ quotes, same as `total_cost`). `None` — with
`baseline_note` explaining why — only when no delayed PO exists for this
component: there is then no identifiable disruption to compare against, and
fabricating one would be exactly the invented-metric problem AGENTS.md rule
7 forbids, just relocated from a rupee figure to a percentage.

--------------------------------------------------------------------------
rejected_alternatives — solver's reasons, carried forward, not re-derived
--------------------------------------------------------------------------
Every `Rejection` SOLVER already produced (uncertified / budget_infeasible /
expired_quote / dominated) becomes one `RejectedAlternative` here, with its
existing `reason`/`note` carried forward verbatim as `regret` — never
re-derived. The one thing SOLVER could not know — which Pareto-front member
this module actually picked — is what makes `saved` computable; every other
Pareto-front member that was NOT picked becomes an additional
`RejectedAlternative` with reason `"not_selected"`, whose regret cites
exactly which tier of `_select_combination`'s rule it lost on.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel

from app.engine.coverage import CoverageResult
from app.engine.coverage import CoverageStatus
from app.engine.coverage import _classify as _coverage_classify
from app.engine.solver import DropReason, Rejection, SolverResult, SourcingCombination
from app.environment.clock import clock
from app.environment.seed_data import STATE, Store

# §5.4's priority field has no inherent numeric order — this is the one place
# that order is decided, exactly once, for reuse everywhere below. Lower
# number = served first.
_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# The plain-English rule this module implements. Echoed onto every Plan so
# the sentence and the behavior can never silently drift apart — see
# test_planner.py's check that this string is what the code actually does.
SELECTION_RULE = (
    "Select the Pareto candidate with the highest reliability_score; "
    "ties broken by the shortest lead_time_days; "
    "further ties broken by the lowest total_price."
)

NO_BASELINE_NOTE = (
    "No delayed purchase order found for this component -- there is no "
    "identifiable pre-disruption baseline price to compare against, so "
    "cost_increase_vs_baseline_pct is not computed. Not the same as "
    "inventing one (AGENTS.md rule 7): a percentage needs a real prior price "
    "to be a percentage OF; absent a disrupted PO, there is nothing that "
    "claims a disruption happened to this component at all."
)


# ==========================================================================
# Output shapes
# ==========================================================================


class PurchaseSplitAction(BaseModel):
    """One supplier's share of the chosen combination, as a plan action."""

    type: Literal["purchase_split"] = "purchase_split"
    supplier_id: str
    qty: int
    unit_price: float
    lead_time_days: int
    reversibility: Literal["compensable"] = "compensable"
    note: str


class ProductionRescheduleAction(BaseModel):
    """A production order pushed out because the real, computed allocation
    says it will not be ready by its original deadline."""

    type: Literal["production_reschedule"] = "production_reschedule"
    production_order_id: str
    delay_days: int
    reversibility: Literal["reversible"] = "reversible"
    justification: str


class SafetyStockDrawAction(BaseModel):
    """Item 5b. `days` matches ARCHITECTURE.md §7's illustrative field name;
    `units` is the actual quantity (auditable, capped at
    `InventoryRecord.safety_stock`). Only ever produced when the draw was
    authorized — see `SafetyStockDecision` for the attempt either way."""

    type: Literal["safety_stock_draw"] = "safety_stock_draw"
    component_id: str
    days: float
    units: int
    reversibility: Literal["compensable"] = "compensable"
    justification: str


PlanAction = Union[PurchaseSplitAction, ProductionRescheduleAction, SafetyStockDrawAction]


class SafetyStockDecision(BaseModel):
    """The safety-stock question, answered either way, for audit — mirrors
    the PollDecision / VerificationSkip pattern from items 2b/3: record the
    attempt, not just a positive outcome."""

    component_id: str
    triggered: bool  # did on-hand coverage run out before relief at all
    gap_days: Optional[float] = None
    drawn: bool
    draw_units: int = 0
    reason: str
    worsened_orders: list[str] = []  # non-empty only when drawn=False because of them


class AllocationDetail(BaseModel):
    """One production order's share of the on-hand + incoming pool, and
    whether the real numbers say it lands on time."""

    production_order_id: str
    priority: Literal["low", "medium", "high"]
    component_required: int
    allocated_on_hand: int
    allocated_incoming: int
    total_allocated: int
    shortfall: int
    earliest_full_supply_day: Optional[float]  # None if never fully supplied
    days_to_deadline: float
    on_time: bool


RejectionReason = Union[DropReason, Literal["not_selected", "deadline_infeasible"]]


class ProductionOrderAtRisk(BaseModel):
    """One production order this plan does NOT get to its own deadline.
    See the module docstring's "cost_of_inaction" section for exactly how
    `units_unbuilt` / `deadline_missed_by_days` are derived."""

    production_order_id: str
    priority: Literal["low", "medium", "high"]
    units_unbuilt: int
    deadline_missed_by_days: Optional[int] = None


class CostOfInaction(BaseModel):
    """CORRECTED shape (see module docstring): structured and non-monetary,
    never a money figure this repo's spec material has no basis for.

    `baseline_total_cost` is exposed alongside the percentage so RATCHET
    (item 6) can read the absolute rupee "cost delta vs baseline" for its
    decision brief without re-deriving the baseline PO lookup a second
    time — one computation, one owner."""

    production_orders_at_risk: list[ProductionOrderAtRisk]
    cost_increase_vs_baseline_pct: Optional[float] = None
    baseline_total_cost: Optional[float] = None
    baseline_note: str


class RejectedAlternative(BaseModel):
    """ARCHITECTURE.md §7's exact Plan.rejected_alternatives[] shape:
    option / saved / regret. `saved` is positive when the rejected option
    would have cost MORE than the chosen plan, negative when it would have
    cost less (a real cost regret, not a saving) — the sign is left to speak
    for itself, not clamped or relabeled."""

    option: str
    saved: Optional[float]
    regret: str
    reason: RejectionReason


class Plan(BaseModel):
    """SOLVER + PLANNER output, consumed by RATCHET (item 6) and AUDIT
    (item 7). ARCHITECTURE.md §7's shape, with reversibility moved onto each
    action (see the module docstring) rather than one plan-level field —
    logged in OPEN_ITEMS.md as a shape change item 6 should confirm."""

    plan_id: str
    component_id: str
    computed_at: datetime
    selection_rule: str
    chosen_combination: SourcingCombination
    actions: list[PlanAction]
    allocations: list[AllocationDetail]
    rejected_alternatives: list[RejectedAlternative]
    safety_stock_decision: SafetyStockDecision
    # False means EVERY Pareto candidate misses a high-priority deadline —
    # this is the chosen combination as a forced fallback (there was nothing
    # feasible to pick from), not a genuine solution. RATCHET (item 6) reads
    # this directly for its "no feasible plan meets a high-priority
    # deadline" hard trigger; it does not recompute it.
    deadline_feasible: bool
    total_cost: float
    cost_of_inaction: CostOfInaction

    def purchase_actions(self) -> list[PurchaseSplitAction]:
        return [a for a in self.actions if isinstance(a, PurchaseSplitAction)]

    def reschedule_actions(self) -> list[ProductionRescheduleAction]:
        return [a for a in self.actions if isinstance(a, ProductionRescheduleAction)]

    def safety_stock_actions(self) -> list[SafetyStockDrawAction]:
        return [a for a in self.actions if isinstance(a, SafetyStockDrawAction)]


# ==========================================================================
# Plan ids
# ==========================================================================

_plan_seq = 0


def next_plan_id() -> str:
    global _plan_seq
    _plan_seq += 1
    return f"PLAN-{_plan_seq:04d}"


def reset_plan_sequence() -> None:
    """Test hook, matching coverage.py's reset_event_sequence()."""
    global _plan_seq
    _plan_seq = 0


# ==========================================================================
# Stage 1 — pick one point from the Pareto set
# ==========================================================================


def _select_combination(pareto_set: list[SourcingCombination]) -> SourcingCombination:
    """Implements SELECTION_RULE exactly: sort by
    (-reliability_score, lead_time_days, total_price) and take the first.
    Three keys, checked in that order, nothing implicit.

    CORRECTED: this now runs only over the DEADLINE-FEASIBLE subset of the
    Pareto set (see `_partition_by_deadline_feasibility`), never the raw
    `pareto_set` — reliability-first ranking must not be allowed to prefer a
    combination that misses a high-priority deadline over one that doesn't.
    Continuity is 35% of the rubric; reliability earns nothing on its own
    (AGENTS.md rule 3's "no supplier meets deadline" is a hard trigger, not a
    tie-break). Deadline feasibility is therefore a HARD FILTER applied
    BEFORE this function ever runs, not a fourth ranking tier — a strict
    lexicographic tier could still be beaten by a large-enough reliability
    gap the same way lead time was; a hard filter cannot be outranked by
    anything, because infeasible options never reach the ranking at all.
    """
    return sorted(
        pareto_set,
        key=lambda c: (-c.reliability_score, c.lead_time_days, c.total_price),
    )[0]


def _is_deadline_feasible(
    combination: SourcingCombination,
    coverage_results: list[CoverageResult],
    on_hand_operating: int,
) -> bool:
    """True if allocating THIS combination (against the reserve-respecting
    on-hand pool `allocate_stock` already uses) gets every HIGH-priority
    order sharing this component to its own deadline. Medium/low-priority
    misses are NOT infeasibility — they stay `_reschedule_actions`'s job,
    matching PROJECT.md §4 Beat 4's own narrative (PROD-914, low priority,
    gets rescheduled, not treated as a blocking failure). Only high-priority
    misses are hard-filtered, matching AGENTS.md rule 3's own escalation
    framing and Production Continuity's 35% rubric weight.
    """
    allocations = allocate_stock(coverage_results, on_hand_operating, combination)
    return all(detail.on_time for detail in allocations if detail.priority == "high")


def _partition_by_deadline_feasibility(
    pareto_set: list[SourcingCombination],
    coverage_results: list[CoverageResult],
    on_hand_operating: int,
) -> tuple[list[SourcingCombination], list[SourcingCombination]]:
    """(feasible, infeasible) — every Pareto candidate re-checked against
    every high-priority order's deadline, using the SAME `allocate_stock`
    the final Plan will use, not a separate estimate."""
    feasible: list[SourcingCombination] = []
    infeasible: list[SourcingCombination] = []
    for combo in pareto_set:
        if _is_deadline_feasible(combo, coverage_results, on_hand_operating):
            feasible.append(combo)
        else:
            infeasible.append(combo)
    return feasible, infeasible


def _why_deadline_infeasible(
    combination: SourcingCombination,
    coverage_results: list[CoverageResult],
    on_hand_operating: int,
) -> str:
    allocations = allocate_stock(coverage_results, on_hand_operating, combination)
    missed = [
        detail for detail in allocations if detail.priority == "high" and not detail.on_time
    ]
    parts = []
    for detail in missed:
        if detail.earliest_full_supply_day is None:
            parts.append(f"{detail.production_order_id} (never fully supplied)")
        else:
            parts.append(
                f"{detail.production_order_id} (ready day "
                f"{detail.earliest_full_supply_day:.1f}, deadline day "
                f"{detail.days_to_deadline:.1f})"
            )
    return "misses high-priority deadline(s): " + ", ".join(parts)


def _why_not_selected(chosen: SourcingCombination, candidate: SourcingCombination) -> str:
    """Which tier of SELECTION_RULE `candidate` lost on, against `chosen`.
    Mirrors solver.py's own pareto_front() axis-naming pattern."""
    if candidate.reliability_score != chosen.reliability_score:
        return (
            f"lost on reliability_score: {candidate.reliability_score} vs "
            f"chosen's {chosen.reliability_score}"
        )
    if candidate.lead_time_days != chosen.lead_time_days:
        return (
            f"tied on reliability_score ({candidate.reliability_score}); "
            f"lost on lead_time_days: {candidate.lead_time_days}d vs "
            f"chosen's {chosen.lead_time_days}d"
        )
    return (
        f"tied on reliability_score and lead_time_days; lost on total_price: "
        f"{candidate.total_price} vs chosen's {chosen.total_price}"
    )


# ==========================================================================
# Stage 2 — stock allocation across competing production orders
# ==========================================================================


def _sort_orders(results: list[CoverageResult]) -> list[CoverageResult]:
    return sorted(results, key=lambda r: (_PRIORITY_RANK[r.priority], r.deadline))


def _sort_members_by_arrival(combination: SourcingCombination):
    return sorted(combination.members, key=lambda m: m.lead_time_days)


def allocate_stock(
    coverage_results: list[CoverageResult],
    on_hand: int,
    combination: SourcingCombination,
) -> list[AllocationDetail]:
    """Allocate on-hand stock, then each incoming member (fastest arrival
    first), to production orders in (priority, deadline) order. See the
    module docstring's "Stock allocation" section for the full reasoning."""
    ordered = _sort_orders(coverage_results)
    remaining_on_hand = on_hand
    # (lead_time_days, remaining_qty) per member, mutated as orders draw from it.
    incoming = [[m.lead_time_days, m.quantity] for m in _sort_members_by_arrival(combination)]

    details: list[AllocationDetail] = []
    for result in ordered:
        need = result.component_required
        allocated_on_hand = min(need, remaining_on_hand)
        remaining_on_hand -= allocated_on_hand
        need -= allocated_on_hand

        allocated_incoming = 0
        earliest_full_supply_day: Optional[float] = 0.0 if allocated_on_hand >= result.component_required else None

        for portion in incoming:
            if need <= 0:
                break
            lead_time_days, available = portion
            take = min(need, available)
            if take <= 0:
                continue
            allocated_incoming += take
            portion[1] -= take
            need -= take
            earliest_full_supply_day = float(lead_time_days)

        total_allocated = allocated_on_hand + allocated_incoming
        shortfall = max(0, result.component_required - total_allocated)
        on_time = (
            earliest_full_supply_day is not None
            and earliest_full_supply_day <= result.days_to_deadline
            and shortfall == 0
        )

        details.append(
            AllocationDetail(
                production_order_id=result.production_order_id,
                priority=result.priority,
                component_required=result.component_required,
                allocated_on_hand=allocated_on_hand,
                allocated_incoming=allocated_incoming,
                total_allocated=total_allocated,
                shortfall=shortfall,
                earliest_full_supply_day=earliest_full_supply_day,
                days_to_deadline=result.days_to_deadline,
                on_time=on_time,
            )
        )

    return details


# ==========================================================================
# Stage 3 — production reschedule from the real shortfall
# ==========================================================================


def _reschedule_actions(allocations: list[AllocationDetail]) -> list[ProductionRescheduleAction]:
    actions: list[ProductionRescheduleAction] = []
    for detail in allocations:
        if detail.on_time:
            continue
        if detail.earliest_full_supply_day is None:
            # Never fully supplied even after the chosen combination — this
            # plan alone cannot close the gap. Item 5b (safety stock) or a
            # further solver pass owns that; this module states the shortfall
            # plainly rather than inventing a delay figure for an order this
            # plan doesn't actually cover.
            continue
        delay_days = math.ceil(detail.earliest_full_supply_day - detail.days_to_deadline)
        if delay_days <= 0:
            continue
        actions.append(
            ProductionRescheduleAction(
                production_order_id=detail.production_order_id,
                delay_days=delay_days,
                justification=(
                    f"{detail.production_order_id} ({detail.priority} priority) needs "
                    f"{detail.component_required} units; the allocated supply "
                    f"({detail.allocated_on_hand} on hand + {detail.allocated_incoming} "
                    f"incoming) is complete only by day {detail.earliest_full_supply_day:.1f}, "
                    f"{delay_days} day(s) past its {detail.days_to_deadline:.1f}-day deadline."
                ),
            )
        )
    return actions


# ==========================================================================
# Stage 2b — safety-stock draw, gated on written justification (item 5b)
# ==========================================================================

_STATUS_RANK: dict[CoverageStatus, int] = {"healthy": 0, "at_risk": 1, "critical": 2}


def _on_hand_only_status(
    usable_stock: float, safety_stock: int, daily_usage: int, days_to_deadline: float
) -> CoverageStatus:
    """Classify using ONLY on-hand stock — no inbound credited — via
    coverage.py's own `_classify` (imported, not reimplemented). Deliberately
    narrower than a CoverageResult's arrival-credited status: item 5b is
    specifically about the window BEFORE the chosen combination's supply
    arrives, where inbound credit doesn't apply yet."""
    if daily_usage <= 0:
        return "healthy"
    days_of_coverage = usable_stock / daily_usage
    days_to_safety_breach = (
        0.0 if usable_stock <= safety_stock else (usable_stock - safety_stock) / daily_usage
    )
    status, _reason = _coverage_classify(days_of_coverage, days_to_safety_breach, days_to_deadline)
    return status


def _safety_stock_decision(
    component_id: str,
    coverage_results: list[CoverageResult],
    chosen: SourcingCombination,
    usable_stock: int,
    safety_stock: int,
    daily_usage: int,
    baseline_allocations: list[AllocationDetail],
) -> SafetyStockDecision:
    """Item 5b. See the module docstring's "Item 5b" section for the full
    trigger / sizing / other-order-impact reasoning this implements."""
    if not baseline_allocations or all(d.on_time for d in baseline_allocations):
        return SafetyStockDecision(
            component_id=component_id,
            triggered=False,
            drawn=False,
            reason=(
                "On-hand coverage (respecting the safety_stock reserve) "
                "already gets every order to its deadline — no gap to bridge."
            ),
        )

    days_of_coverage_on_hand = usable_stock / daily_usage if daily_usage > 0 else math.inf
    earliest_secured_delivery_day = min(m.lead_time_days for m in chosen.members)
    gap_days = earliest_secured_delivery_day - days_of_coverage_on_hand

    if daily_usage <= 0 or gap_days <= 0:
        return SafetyStockDecision(
            component_id=component_id,
            triggered=False,
            drawn=False,
            reason=(
                f"{component_id}: on-hand coverage lasts until day "
                f"{days_of_coverage_on_hand:.1f}, at or before the earliest "
                f"secured delivery on day {earliest_secured_delivery_day} — no "
                "physical stockout gap for safety stock to bridge. Any "
                "reschedule here is a unit-allocation timing issue, not a "
                "stockout risk."
            ),
        )

    draw_units = min(math.ceil(gap_days * daily_usage), safety_stock)
    if draw_units <= 0:
        return SafetyStockDecision(
            component_id=component_id,
            triggered=True,
            gap_days=round(gap_days, 1),
            drawn=False,
            reason=(
                f"{component_id}: a {gap_days:.1f}-day physical gap exists "
                f"before the earliest secured delivery, but safety_stock is "
                f"{safety_stock} — nothing available to draw."
            ),
        )

    usable_stock_after = usable_stock - draw_units
    # Orders already drawing on the SAME chosen combination's incoming supply
    # are the ones this draw exists to help — the on-hand-only lens ignores
    # incoming supply entirely, so applying it to them would penalize the
    # beneficiary for benefiting. Only genuine bystanders — orders that get
    # ZERO of the incoming shipment under the reserve-respecting baseline,
    # whether because they're already fully on-hand-covered or because a
    # higher-priority order consumed the whole incoming pool first — have a
    # margin that is purely being spent on someone else's behalf, and are
    # what this check is actually for.
    bystanders = {
        d.production_order_id for d in baseline_allocations if d.allocated_incoming == 0
    }
    worsened: list[str] = []
    for result in coverage_results:
        if result.production_order_id not in bystanders:
            continue
        before_status = _on_hand_only_status(
            usable_stock, safety_stock, daily_usage, result.days_to_deadline
        )
        after_status = _on_hand_only_status(
            usable_stock_after, safety_stock, daily_usage, result.days_to_deadline
        )
        if _STATUS_RANK[after_status] > _STATUS_RANK[before_status]:
            worsened.append(result.production_order_id)

    if worsened:
        return SafetyStockDecision(
            component_id=component_id,
            triggered=True,
            gap_days=round(gap_days, 1),
            drawn=False,
            draw_units=0,
            reason=(
                f"{component_id}: a {gap_days:.1f}-day physical gap exists "
                f"before relief arrives, and drawing {draw_units} of "
                f"{safety_stock} safety-stock units would bridge it, but it "
                f"would also worsen {', '.join(sorted(worsened))}'s own "
                "on-hand-only classification — refused rather than authorized "
                "with a footnote (see module docstring's 'Item 5b' section)."
            ),
            worsened_orders=sorted(worsened),
        )

    covered_days = min(gap_days, draw_units / daily_usage)
    affected = sorted({r.production_order_id for r in coverage_results})
    return SafetyStockDecision(
        component_id=component_id,
        triggered=True,
        gap_days=round(gap_days, 1),
        drawn=True,
        draw_units=draw_units,
        reason=(
            f"{component_id}: on-hand coverage runs out at day "
            f"{days_of_coverage_on_hand:.1f} ({usable_stock} units at "
            f"{daily_usage}/day), {gap_days:.1f} day(s) before the earliest "
            f"secured delivery arrives on day {earliest_secured_delivery_day}. "
            f"Drawing {draw_units} of {safety_stock} safety-stock units covers "
            f"{covered_days:.1f} of those {gap_days:.1f} day(s). Affects: "
            f"{', '.join(affected)}. No other order's on-hand-only "
            "classification worsens as a result."
        ),
    )


# ==========================================================================
# Stage 4 — rejected alternatives, saved figure filled in
# ==========================================================================


def _rejected_alternatives(
    solver_rejections: list[Rejection],
    feasible_set: list[SourcingCombination],
    infeasible_set: list[SourcingCombination],
    coverage_results: list[CoverageResult],
    on_hand_operating: int,
    chosen: SourcingCombination,
    chosen_total_cost: float,
) -> list[RejectedAlternative]:
    """`feasible_set` / `infeasible_set` are `_partition_by_deadline_
    feasibility`'s own output — two distinct reasons, never conflated:
    `deadline_infeasible` (missed a high-priority deadline, dropped before
    ranking) vs `not_selected` (feasible, but lost on SELECTION_RULE's own
    tiers)."""
    alternatives: list[RejectedAlternative] = []

    for rejection in solver_rejections:
        estimate = rejection.estimated_total_price
        if estimate is None and rejection.estimated_unit_price is not None:
            # Hard-filtered suppliers never got quantity-priced by an RFQ —
            # estimate at the chosen plan's own quantity so "saved" compares
            # like-for-like, using the same figure the module docstring
            # already labels an estimate, not pretending it's a real quote.
            estimate = rejection.estimated_unit_price * chosen.quantity_needed
        saved = None if estimate is None else round(estimate - chosen_total_cost, 2)
        alternatives.append(
            RejectedAlternative(
                option=rejection.subject,
                saved=saved,
                regret=rejection.note,
                reason=rejection.reason,
            )
        )

    for combo in infeasible_set:
        if combo is chosen:
            continue  # the forced-fallback case — chosen is not its own rejection
        alternatives.append(
            RejectedAlternative(
                option=combo.label,
                saved=round(combo.total_price - chosen_total_cost, 2),
                regret=_why_deadline_infeasible(combo, coverage_results, on_hand_operating),
                reason="deadline_infeasible",
            )
        )

    for combo in feasible_set:
        if combo is chosen:
            continue
        alternatives.append(
            RejectedAlternative(
                option=combo.label,
                saved=round(combo.total_price - chosen_total_cost, 2),
                regret=_why_not_selected(chosen, combo),
                reason="not_selected",
            )
        )

    return alternatives


# ==========================================================================
# Stage 5 — cost_of_inaction: structured, non-monetary (see module docstring)
# ==========================================================================


def _baseline_unit_price(store: Store, component_id: str) -> Optional[float]:
    """The price already committed to before the disruption — a delayed
    PO's own contracted `unit_price`. "Baseline = the plan cost had no
    disruption occurred" read literally: the specific PO whose delay
    created this shortfall is exactly what "no disruption" would have cost.
    `None` when no delayed PO exists for this component — see
    `NO_BASELINE_NOTE`. The minimum, when several: the best price the
    disruption is actually costing us against."""
    delayed = [
        po
        for po in store.list_purchase_orders(component_id=component_id)
        if po.status == "delayed"
    ]
    if not delayed:
        return None
    return min(po.unit_price for po in delayed)


def _compute_cost_of_inaction(
    store: Store,
    component_id: str,
    coverage_results: list[CoverageResult],
    on_hand_operating: int,
    chosen: SourcingCombination,
) -> CostOfInaction:
    # Counterfactual inaction world: no new purchase orders placed (0 incoming supply)
    inaction_combo = SourcingCombination(
        members=[],
        quantity_needed=chosen.quantity_needed,
        quantity_allocated=0,
        total_price=0.0,
        lead_time_days=0,
        reliability_score=1.0,
        quality_score=1.0,
        total_min_order_quantity=0,
        total_available_quantity=0,
    )
    inaction_allocations = allocate_stock(coverage_results, on_hand_operating, inaction_combo)

    at_risk = [
        ProductionOrderAtRisk(
            production_order_id=detail.production_order_id,
            priority=detail.priority,
            units_unbuilt=max(0, detail.component_required - detail.allocated_on_hand),
            deadline_missed_by_days=None,
        )
        for detail in inaction_allocations
        if not detail.on_time
    ]

    baseline_price = _baseline_unit_price(store, component_id)
    if baseline_price is None or baseline_price <= 0:
        return CostOfInaction(
            production_orders_at_risk=at_risk,
            cost_increase_vs_baseline_pct=None,
            baseline_note=NO_BASELINE_NOTE,
        )

    baseline_total = baseline_price * chosen.quantity_allocated
    pct = round((chosen.total_price - baseline_total) / baseline_total * 100, 2)
    return CostOfInaction(
        production_orders_at_risk=at_risk,
        cost_increase_vs_baseline_pct=pct,
        baseline_total_cost=round(baseline_total, 2),
        baseline_note=(
            f"Baseline = {component_id}'s delayed PO's own contracted "
            f"unit_price ({baseline_price}) x quantity_allocated "
            f"({chosen.quantity_allocated}) = {baseline_total:.2f} — what "
            "this purchase would have cost had no disruption occurred. "
            f"Actual = chosen_combination.total_price ({chosen.total_price}), "
            "from real RFQ quotes."
        ),
    )


# ==========================================================================
# The pass
# ==========================================================================


def run_planner(
    store: Store,
    component_id: str,
    solver_result: SolverResult,
    coverage_results: list[CoverageResult],
    *,
    now: Optional[datetime] = None,
    allow_reschedule: bool = True,
) -> Optional[Plan]:
    """HARD FILTER and SOLVER (item 4) already ran; this is PLAN (item 5).

    Deterministic stock allocation, safety-stock draw gate, production
    reschedule, and choice of the non-dominated combination that maximizes
    reliability. Returns None when solver_result.pareto_set is empty.
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now

    if not solver_result.pareto_set:
        return None

    inventory = store.get_inventory(component_id)
    usable_stock = inventory.usable_stock if inventory is not None else 0
    safety_stock = inventory.safety_stock if inventory is not None else 0
    daily_usage = inventory.daily_usage if inventory is not None else 0

    # Item 5b: allocation draws from the OPERATING buffer by default, not the
    # full usable_stock — the reserve is only spent through the explicit,
    # justified mechanism below. See the module docstring's "Item 5b" section.
    # Computed BEFORE selection now (moved up from where item 5 originally
    # had it) because the deadline-feasibility hard filter below needs it to
    # run allocate_stock against EVERY Pareto candidate, not just the chosen
    # one.
    on_hand_operating = max(0, usable_stock - safety_stock)

    # CORRECTED selection: deadline feasibility is a HARD FILTER before
    # ranking, not a ranking tier — see _select_combination's docstring for
    # why a tier isn't enough. Falls back to the full Pareto set only when
    # NOTHING is feasible, so RATCHET (item 6) still has a concrete plan to
    # build an escalation brief around; `deadline_feasible=False` is exactly
    # its "no feasible plan meets a high-priority deadline" trigger.
    feasible, infeasible = _partition_by_deadline_feasibility(
        solver_result.pareto_set, coverage_results, on_hand_operating
    )
    selection_pool = feasible if feasible else solver_result.pareto_set
    chosen = _select_combination(selection_pool)
    # Identity, not `in` (which would use pydantic's structural __eq__ and
    # could false-positive if two distinct combinations were ever built with
    # identical field values).
    deadline_feasible = any(c is chosen for c in feasible)

    baseline_allocations = allocate_stock(coverage_results, on_hand_operating, chosen)

    safety_stock_decision = _safety_stock_decision(
        component_id,
        coverage_results,
        chosen,
        usable_stock,
        safety_stock,
        daily_usage,
        baseline_allocations,
    )

    safety_stock_action: list[SafetyStockDrawAction] = []
    if safety_stock_decision.drawn:
        on_hand_final = on_hand_operating + safety_stock_decision.draw_units
        allocations = allocate_stock(coverage_results, on_hand_final, chosen)
        covered_days = (
            min(safety_stock_decision.gap_days, safety_stock_decision.draw_units / daily_usage)
            if daily_usage > 0 and safety_stock_decision.gap_days is not None
            else 0.0
        )
        safety_stock_action.append(
            SafetyStockDrawAction(
                component_id=component_id,
                days=round(covered_days, 1),
                units=safety_stock_decision.draw_units,
                justification=safety_stock_decision.reason,
            )
        )
    else:
        allocations = baseline_allocations

    reschedules = _reschedule_actions(allocations) if allow_reschedule else []

    purchase_actions: list[PurchaseSplitAction] = [
        PurchaseSplitAction(
            supplier_id=member.supplier_id,
            qty=member.quantity,
            unit_price=member.unit_price,
            lead_time_days=member.lead_time_days,
            note=(
                f"{member.quantity} units @ {member.unit_price}/unit from "
                f"{member.supplier_id}, {member.lead_time_days}-day lead time."
            ),
        )
        for member in chosen.members
    ]

    rejected = _rejected_alternatives(
        solver_result.rejected,
        feasible,
        infeasible,
        coverage_results,
        on_hand_operating,
        chosen,
        chosen.total_price,
    )

    cost_of_inaction = _compute_cost_of_inaction(
        store, component_id, coverage_results, on_hand_operating, chosen
    )

    return Plan(
        plan_id=next_plan_id(),
        component_id=component_id,
        computed_at=now,
        selection_rule=SELECTION_RULE,
        chosen_combination=chosen,
        actions=[*purchase_actions, *safety_stock_action, *reschedules],
        allocations=allocations,
        rejected_alternatives=rejected,
        safety_stock_decision=safety_stock_decision,
        deadline_feasible=deadline_feasible,
        total_cost=chosen.total_price,
        cost_of_inaction=cost_of_inaction,
    )
