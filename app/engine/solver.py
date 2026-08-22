"""SOLVER — ARCHITECTURE.md §4 item 4, §3's fourth and fifth stages (HARD
FILTER, then SOLVER). Serves Cost Control (20%) and Supplier Risk Handling
(15%, PROJECT.md §2).

Deterministic throughout — brute-force enumeration and comparison, no LLM
anywhere (AGENTS.md rule 1). This module decides WHICH supplier options are
even eligible and WHICH of those are Pareto-optimal; it does not decide which
one to execute (RATCHET, item 6) or how to shape a multi-action plan around
one (PLANNER, item 5).

--------------------------------------------------------------------------
Two stages, in the order ARCHITECTURE.md §3's diagram gives them
--------------------------------------------------------------------------
1. HARD FILTER — drop uncertified or budget-infeasible suppliers using data
   already on `GET /suppliers` (§5.3), BEFORE spending an RFQ call on them.
   Both checks are spec-field comparisons, not judgment calls (see
   `_is_certified` / `_is_budget_feasible` for exactly which fields and why).
2. SOLVER proper — request a live quote (§5.7) for each survivor, drop any
   quote past `quote_valid_hours`, brute-force enumerate feasible ways to
   source `quantity_needed` from the survivors (single-supplier fills and
   2-supplier splits — ARCHITECTURE.md §1's own justification: "~120
   combinations for ≤2-of-14 suppliers is exact and fast"), then take the
   Pareto non-dominated set over six axes: price, lead time, reliability,
   quality, MOQ, available quantity. No weighted-sum collapse — that is
   exactly the rejected Trust Gate's mistake (AGENTS.md rule 7,
   PROJECT.md §7 Standing Decisions), and this module does not reintroduce
   it under a friendlier name. Nothing here computes a single "score";
   `_dominates` compares six numbers to six numbers, axis by axis.

--------------------------------------------------------------------------
Reliability is read live, not defaulted
--------------------------------------------------------------------------
Every dimension is read straight off the live `Store` at solve time —
`store.suppliers[supplier_id].reliability_score`, not a value this module
carries or recomputes. VERIFY (item 3) mutates that same field in place on a
tracking contradiction (`app/engine/verify.py::_update_reliability`), so a
solve that runs after a verification cycle automatically sees the
post-verification score. This is the entire mechanism behind the "low-
reliability supplier is fastest" hidden test (ARCHITECTURE.md §10): SUP-21 is
the fastest certified COMP-104 supplier (3-day lead time) and, once item 3
downgrades it after the PO-7712 contradiction, also the least reliable one —
Pareto must still surface it, not silently drop it or average it away.

--------------------------------------------------------------------------
Two named hard-filter rules, and what each is actually grounded in
--------------------------------------------------------------------------
**Certified** — a supplier passes if `certifications` is non-empty.
`seed_data.py`'s own comment on SUP-18 (`certifications=[]  # fails the
certified-supplier requirement, §6`) and ARCHITECTURE.md §7's worked
rejected-alternative (`"regret": "disqualified — uncertified"`) both describe
the failure as *lacking certification entirely*, not lacking one specific
credential — so "certified" is read here as "holds at least one
certification," not "holds ISO-9001 specifically." This repo does not have
the literal problem-statement text to check further; if that reading is
wrong, the fix is a one-line change to `_is_certified`, not a design change.

**Budget-infeasible** — a supplier's *minimum viable commitment*
(`unit_price × min_order_quantity`, the smallest purchase actually possible
from them) exceeds the applicable approval threshold, checked by calling the
same `POST /approval/check` logic (§5.8) the environment already exposes
(`store.check_approval`) rather than re-deriving threshold selection here.
This is deliberately NOT "this supplier's price for the full quantity needed
exceeds the threshold": that would be a plan-level cost question, which is
RATCHET's job on the FINAL SELECTED plan (AGENTS.md rule 3), and checking it
here would silently prevent order-splitting (item 5) from ever using a
supplier whose full-quantity price is expensive but whose PARTIAL
contribution to a split is fine. Exceeding the threshold is not "infeasible"
— a human can approve it — but a supplier whose SMALLEST possible order
already needs escalation can never be part of an autonomously-executable
candidate, so it does not survive to spend an RFQ call proving that again.

--------------------------------------------------------------------------
quote_valid_hours is enforced, not displayed
--------------------------------------------------------------------------
A quote that has passed `quote_issued_at + quote_valid_hours` is dropped from
the candidate set with the same finality as a cert failure — reason
`expired_quote`. Pass `existing_quotes` to reuse quotes an earlier cycle
already fetched (tool efficiency — no repeat RFQ call for a still-valid
quote); an expired one is dropped, not silently auto-refreshed, because
re-quoting is itself a deliberate decision the caller should make, not
something this module does implicitly on a stale read.

--------------------------------------------------------------------------
What this module hands to item 5
--------------------------------------------------------------------------
`SolverResult.pareto_set` — the non-dominated combinations, each carrying
every dimension needed to build a `Plan` action. `SolverResult.rejected` —
every dropped supplier AND every dominated combination, each tagged with a
reason (`uncertified | budget_infeasible | expired_quote | dominated`) and
the comparable figures behind it (estimated price, lead time, reliability,
quality). This module does NOT compute a dollar "regret" figure
(ARCHITECTURE.md §7's `Plan.rejected_alternatives[].saved`) — that requires
knowing which option item 5/6 actually selects, a decision this module does
not make. Item 5 computes `saved` against whatever it picks; it reads the
comparable figures from here instead of re-deriving them.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel

from app.environment.clock import clock
from app.environment.schemas import (
    ApprovalCheckRequest,
    RFQQuote,
    RFQRequest,
    SupplierRecord,
)
from app.environment.seed_data import STATE, Store

DropReason = Literal["uncertified", "budget_infeasible", "expired_quote", "dominated"]

# The maximum number of suppliers combined into one sourcing option.
# ARCHITECTURE.md §1's own justification for brute force over MILP/OR-Tools:
# "~120 combinations for ≤2-of-14 suppliers is exact and fast." Raising this
# is a real scope change (combinatorial growth), not a config tweak.
MAX_COMBINATION_SIZE = 2


# ==========================================================================
# Output shapes
# ==========================================================================


class SupplierOption(BaseModel):
    """One supplier's contribution within a sourcing combination — the unit
    both single-supplier and 2-way-split combinations are built from.

    `quantity` is what this member actually contributes in this combination;
    `available_quantity` is its ceiling (the quote's `quantity_available`,
    already capped by the environment at `quantity_needed` — see
    `_generate_single_supplier_combinations`'s comment on that cap). The two
    differ exactly when a member contributes less than it could have, which
    is normal for the cheaper half of a 2-way split.
    """

    supplier_id: str
    unit_price: float
    quantity: int
    available_quantity: int
    lead_time_days: int
    reliability_score: float
    quality_score: float
    min_order_quantity: int


class SourcingCombination(BaseModel):
    """One feasible way to source `quantity_needed` units — one supplier
    alone, or two supplers split. The unit Pareto non-domination compares.

    Per-axis rollups, each chosen to stay a literal, non-invented aggregation
    of ONE dimension at a time — never a cross-dimension blend (that is the
    weighted-sum collapse this module exists to avoid):
      - total_price: sum(qty_i * unit_price_i) — literal total spend.
      - lead_time_days: max over members — the combination isn't complete
        until its slowest delivery lands.
      - reliability_score: min over members — a split's risk is bounded by
        its weakest-link supplier, not its average.
      - quality_score: min over members — same reasoning.
      - total_min_order_quantity: sum of each contributing member's own MOQ —
        the combination's total binding commitment; lower means more
        flexibility if this exact combination is not re-used later.
      - total_available_quantity: sum of each member's available quantity —
        the combination's total headroom against a further shortfall.
    """

    members: list[SupplierOption]
    quantity_needed: int
    quantity_allocated: int
    total_price: float
    lead_time_days: int
    reliability_score: float
    quality_score: float
    total_min_order_quantity: int
    total_available_quantity: int

    @property
    def supplier_ids(self) -> list[str]:
        return [m.supplier_id for m in self.members]

    @property
    def label(self) -> str:
        if len(self.members) == 1:
            return f"{self.members[0].supplier_id} only"
        return " + ".join(
            f"{m.supplier_id} ({m.quantity})" for m in self.members
        )


class Rejection(BaseModel):
    """One dropped candidate — a supplier (cert/budget/expiry) or a
    combination (dominated) — with the reason and the comparable figures
    item 5 needs to compute its own regret against whatever it selects.
    Never a computed dollar "saved" figure — see the module docstring."""

    subject: str  # supplier_id, or a combination's SourcingCombination.label
    reason: DropReason
    note: str
    estimated_unit_price: Optional[float] = None
    estimated_total_price: Optional[float] = None
    lead_time_days: Optional[int] = None
    reliability_score: Optional[float] = None
    quality_score: Optional[float] = None
    combination: Optional[SourcingCombination] = None


class SolverResult(BaseModel):
    """One HARD FILTER + SOLVER pass for one component."""

    computed_at: datetime
    component_id: str
    quantity_needed: int
    pareto_set: list[SourcingCombination]
    rejected: list[Rejection]
    quotes_requested: int
    quotes_reused: int

    def dropped_for(self, reason: DropReason) -> list[Rejection]:
        return [r for r in self.rejected if r.reason == reason]

    def cheapest(self) -> Optional[SourcingCombination]:
        if not self.pareto_set:
            return None
        return min(self.pareto_set, key=lambda c: c.total_price)

    def fastest(self) -> Optional[SourcingCombination]:
        if not self.pareto_set:
            return None
        return min(self.pareto_set, key=lambda c: c.lead_time_days)


# ==========================================================================
# Stage 1 — HARD FILTER
# ==========================================================================


def _is_certified(supplier: SupplierRecord) -> bool:
    """Holds at least one certification. See the module docstring's
    "Certified" section for exactly what this is grounded in."""
    return len(supplier.certifications) > 0


def _min_commitment_cost(supplier: SupplierRecord) -> float:
    """The smallest purchase actually possible from this supplier — the
    figure the budget hard-filter checks, not the full quantity needed
    (see the module docstring's "Budget-infeasible" section for why)."""
    return supplier.unit_price * supplier.min_order_quantity


def hard_filter(
    store: Store, component_id: str, *, po_id: Optional[str] = None
) -> tuple[list[SupplierRecord], list[Rejection]]:
    """Drop uncertified or budget-infeasible suppliers using `GET /suppliers`
    data alone — no RFQ call spent on anyone who doesn't survive this.

    Budget feasibility reuses `store.check_approval` (§5.8) for threshold
    selection (the named PO's `approval_required_above`, else the global
    default) rather than re-deriving that rule here.
    """
    survivors: list[SupplierRecord] = []
    rejections: list[Rejection] = []

    for supplier in sorted(
        store.list_suppliers(component_id=component_id), key=lambda s: s.supplier_id
    ):
        if not _is_certified(supplier):
            rejections.append(
                Rejection(
                    subject=supplier.supplier_id,
                    reason="uncertified",
                    note=(
                        f"{supplier.supplier_id} holds no certifications — "
                        "dropped before spending an RFQ call."
                    ),
                    estimated_unit_price=supplier.unit_price,
                    lead_time_days=supplier.lead_time_days,
                    reliability_score=supplier.reliability_score,
                    quality_score=supplier.quality_score,
                )
            )
            continue

        min_cost = _min_commitment_cost(supplier)
        approval = store.check_approval(
            ApprovalCheckRequest(
                action="hard_filter_budget_check",
                estimated_cost=min_cost,
                po_id=po_id,
            )
        )
        if approval.approval_required:
            rejections.append(
                Rejection(
                    subject=supplier.supplier_id,
                    reason="budget_infeasible",
                    note=(
                        f"{supplier.supplier_id}'s minimum order "
                        f"({supplier.min_order_quantity} units @ "
                        f"{supplier.unit_price}) costs {min_cost:.0f}, which "
                        f"already needs approval ({approval.approval_reason}) "
                        "— dropped before spending an RFQ call."
                    ),
                    estimated_unit_price=supplier.unit_price,
                    estimated_total_price=min_cost,
                    lead_time_days=supplier.lead_time_days,
                    reliability_score=supplier.reliability_score,
                    quality_score=supplier.quality_score,
                )
            )
            continue

        survivors.append(supplier)

    return survivors, rejections


# ==========================================================================
# Stage 2a — RFQ + quote expiry
# ==========================================================================


def _is_quote_valid(quote: RFQQuote, now: datetime) -> bool:
    expires_at = quote.quote_issued_at + timedelta(hours=quote.quote_valid_hours)
    return now <= expires_at


def fetch_valid_quotes(
    store: Store,
    survivors: list[SupplierRecord],
    quantity_needed: int,
    *,
    existing_quotes: Optional[dict[str, RFQQuote]] = None,
    now: Optional[datetime] = None,
) -> tuple[dict[str, RFQQuote], list[Rejection], int, int]:
    """One RFQ call per survivor that doesn't already have a still-valid
    quote — never a broad, unscoped RFQ call, so a hard-filtered-out
    supplier's disqualification is never spent on anyway (that would defeat
    the entire point of filtering before RFQ).

    A supplied `existing_quotes[supplier_id]` past `quote_valid_hours` is
    dropped (`expired_quote`) rather than silently auto-refreshed — a stale
    quote enforces the constraint by disappearing, not by being quietly
    replaced.
    """
    now = clock.now() if now is None else now
    existing_quotes = existing_quotes or {}

    valid: dict[str, RFQQuote] = {}
    rejections: list[Rejection] = []
    requested = 0
    reused = 0

    for supplier in survivors:
        prior = existing_quotes.get(supplier.supplier_id)
        if prior is not None:
            if _is_quote_valid(prior, now):
                valid[supplier.supplier_id] = prior
                reused += 1
                continue
            rejections.append(
                Rejection(
                    subject=supplier.supplier_id,
                    reason="expired_quote",
                    note=(
                        f"{supplier.supplier_id}'s quote was issued at "
                        f"{prior.quote_issued_at.isoformat()}, valid "
                        f"{prior.quote_valid_hours}h — expired as of "
                        f"{now.isoformat()}."
                    ),
                    estimated_unit_price=prior.unit_price,
                    lead_time_days=prior.delivery_days,
                    reliability_score=supplier.reliability_score,
                    quality_score=supplier.quality_score,
                )
            )
            continue

        quotes = store.request_rfq(
            RFQRequest(
                component_id=supplier.component_id,
                quantity=quantity_needed,
                supplier_id=supplier.supplier_id,
            )
        )
        requested += 1
        if not quotes:
            continue
        quote = quotes[0]
        if _is_quote_valid(quote, now):
            valid[supplier.supplier_id] = quote
        else:
            # Only reachable if quote_valid_hours were ever 0 — a freshly
            # issued quote (quote_issued_at == now) cannot already be
            # expired. Handled rather than assumed impossible.
            rejections.append(
                Rejection(
                    subject=supplier.supplier_id,
                    reason="expired_quote",
                    note=f"{supplier.supplier_id}'s freshly issued quote was already past its validity window.",
                    estimated_unit_price=quote.unit_price,
                    lead_time_days=quote.delivery_days,
                    reliability_score=supplier.reliability_score,
                    quality_score=supplier.quality_score,
                )
            )

    return valid, rejections, requested, reused


# ==========================================================================
# Stage 2b — combination enumeration
# ==========================================================================


def _build_option(supplier: SupplierRecord, quote: RFQQuote, quantity: int) -> SupplierOption:
    return SupplierOption(
        supplier_id=supplier.supplier_id,
        unit_price=quote.unit_price,
        quantity=quantity,
        available_quantity=quote.quantity_available,
        lead_time_days=quote.delivery_days,
        reliability_score=supplier.reliability_score,
        quality_score=supplier.quality_score,
        min_order_quantity=supplier.min_order_quantity,
    )


def _combine(options: list[SupplierOption], quantity_needed: int) -> SourcingCombination:
    total_price = sum(o.unit_price * o.quantity for o in options)
    return SourcingCombination(
        members=options,
        quantity_needed=quantity_needed,
        quantity_allocated=sum(o.quantity for o in options),
        total_price=round(total_price, 2),
        lead_time_days=max(o.lead_time_days for o in options),
        reliability_score=min(o.reliability_score for o in options),
        quality_score=min(o.quality_score for o in options),
        total_min_order_quantity=sum(o.min_order_quantity for o in options),
        total_available_quantity=sum(o.available_quantity for o in options),
    )


def _generate_single_supplier_combinations(
    survivors: list[SupplierRecord],
    quotes: dict[str, RFQQuote],
    quantity_needed: int,
) -> list[SourcingCombination]:
    combinations: list[SourcingCombination] = []
    for supplier in survivors:
        quote = quotes.get(supplier.supplier_id)
        if quote is None:
            continue
        # quote.quantity_available is min(raw available_quantity, requested
        # quantity) — since every quote here was requested at
        # quantity_needed, quantity_available == quantity_needed exactly
        # when the supplier alone can cover it. A partial value means this
        # supplier alone cannot, and only qualifies as a combination member.
        if quote.quantity_available < quantity_needed:
            continue
        if quantity_needed < supplier.min_order_quantity:
            continue
        option = _build_option(supplier, quote, quantity_needed)
        combinations.append(_combine([option], quantity_needed))
    return combinations


def _allocate_pair(
    a: SupplierRecord,
    quote_a: RFQQuote,
    b: SupplierRecord,
    quote_b: RFQQuote,
    quantity_needed: int,
) -> Optional[SourcingCombination]:
    """Cheapest-first allocation within a fixed pair — a single-dimension
    (price) tie-break used only to produce ONE concrete, evaluable
    combination for this pair. This is not a claim that it's the only valid
    split, and it is not a cross-dimension score: it decides quantities
    within a pair using price alone, exactly the way a buyer would default
    to "fill the cheaper one first," and item 5 (PLANNER) may choose a
    different allocation within the same pair.
    """
    cheap, cheap_quote, costly, costly_quote = (
        (a, quote_a, b, quote_b)
        if quote_a.unit_price <= quote_b.unit_price
        else (b, quote_b, a, quote_a)
    )

    cheap_alloc = min(cheap_quote.quantity_available, quantity_needed)
    if 0 < cheap_alloc < cheap.min_order_quantity:
        cheap_alloc = 0  # can't meaningfully contribute below its own MOQ

    remainder = quantity_needed - cheap_alloc
    if remainder <= 0:
        return None  # cheap alone suffices — already covered by size-1 case

    costly_alloc = max(remainder, costly.min_order_quantity)
    if costly_alloc > costly_quote.quantity_available:
        return None  # costly can't cover the remainder even at its ceiling
    if cheap_alloc > 0 and cheap_alloc < cheap.min_order_quantity:
        return None  # defensive — see the check above

    options = []
    if cheap_alloc > 0:
        options.append(_build_option(cheap, cheap_quote, cheap_alloc))
    options.append(_build_option(costly, costly_quote, costly_alloc))
    if len(options) < 2:
        return None  # cheap contributed nothing — this degenerates to size-1

    return _combine(options, quantity_needed)


def _generate_pair_combinations(
    survivors: list[SupplierRecord],
    quotes: dict[str, RFQQuote],
    quantity_needed: int,
) -> list[SourcingCombination]:
    combinations: list[SourcingCombination] = []
    quoted = [s for s in survivors if s.supplier_id in quotes]
    for a, b in itertools.combinations(quoted, 2):
        combo = _allocate_pair(a, quotes[a.supplier_id], b, quotes[b.supplier_id], quantity_needed)
        if combo is not None:
            combinations.append(combo)
    return combinations


# ==========================================================================
# Stage 2c — Pareto non-domination
# ==========================================================================


def _dominates(x: SourcingCombination, y: SourcingCombination) -> bool:
    """True if x is at least as good as y on every axis and strictly better
    on at least one. Six independent comparisons — never collapsed into a
    single number (AGENTS.md rule 7; this is the exact mechanism the
    rejected Trust Gate got wrong)."""
    at_least_as_good = (
        x.total_price <= y.total_price
        and x.lead_time_days <= y.lead_time_days
        and x.reliability_score >= y.reliability_score
        and x.quality_score >= y.quality_score
        and x.total_min_order_quantity <= y.total_min_order_quantity
        and x.total_available_quantity >= y.total_available_quantity
    )
    strictly_better = (
        x.total_price < y.total_price
        or x.lead_time_days < y.lead_time_days
        or x.reliability_score > y.reliability_score
        or x.quality_score > y.quality_score
        or x.total_min_order_quantity < y.total_min_order_quantity
        or x.total_available_quantity > y.total_available_quantity
    )
    return at_least_as_good and strictly_better


def pareto_front(
    combinations: list[SourcingCombination],
) -> tuple[list[SourcingCombination], list[Rejection]]:
    """Non-dominated combinations, plus a Rejection (reason="dominated") for
    each one beaten — naming which combination beat it and on which axes, so
    "dominated" is a legible finding, not a bare label."""
    front: list[SourcingCombination] = []
    dominated: list[Rejection] = []

    for candidate in combinations:
        beaten_by = next(
            (other for other in combinations if other is not candidate and _dominates(other, candidate)),
            None,
        )
        if beaten_by is None:
            front.append(candidate)
            continue

        axes = []
        if beaten_by.total_price < candidate.total_price:
            axes.append(f"price {beaten_by.total_price} < {candidate.total_price}")
        if beaten_by.lead_time_days < candidate.lead_time_days:
            axes.append(f"lead time {beaten_by.lead_time_days}d < {candidate.lead_time_days}d")
        if beaten_by.reliability_score > candidate.reliability_score:
            axes.append(f"reliability {beaten_by.reliability_score} > {candidate.reliability_score}")
        if beaten_by.quality_score > candidate.quality_score:
            axes.append(f"quality {beaten_by.quality_score} > {candidate.quality_score}")
        if beaten_by.total_min_order_quantity < candidate.total_min_order_quantity:
            axes.append("lower total MOQ")
        if beaten_by.total_available_quantity > candidate.total_available_quantity:
            axes.append("more available quantity")

        dominated.append(
            Rejection(
                subject=candidate.label,
                reason="dominated",
                note=(
                    f"{candidate.label} is dominated by {beaten_by.label} on "
                    + ", ".join(axes)
                    + ", and no worse on every other axis."
                ),
                estimated_total_price=candidate.total_price,
                lead_time_days=candidate.lead_time_days,
                reliability_score=candidate.reliability_score,
                quality_score=candidate.quality_score,
                combination=candidate,
            )
        )

    return front, dominated


# ==========================================================================
# The pass
# ==========================================================================


def run_solver(
    store: Optional[Store] = None,
    component_id: str = "",
    quantity_needed: int = 0,
    *,
    po_id: Optional[str] = None,
    existing_quotes: Optional[dict[str, RFQQuote]] = None,
    now: Optional[datetime] = None,
) -> SolverResult:
    """HARD FILTER, then RFQ + expiry, then brute-force combination
    enumeration, then Pareto non-domination. One call, the full item-4 pass.

    `quantity_needed` is a caller-supplied input, not derived here — turning
    a coverage shortfall into a specific unit quantity is a planning decision
    (item 5's), not this module's; this mirrors how `RFQRequest.quantity`
    (§5.7) is itself a caller-supplied field, not something the environment
    infers.
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now

    survivors, hard_filter_rejections = hard_filter(store, component_id, po_id=po_id)

    quotes, expiry_rejections, requested, reused = fetch_valid_quotes(
        store, survivors, quantity_needed, existing_quotes=existing_quotes, now=now
    )
    quoted_survivors = [s for s in survivors if s.supplier_id in quotes]

    combinations = _generate_single_supplier_combinations(
        quoted_survivors, quotes, quantity_needed
    )
    if MAX_COMBINATION_SIZE >= 2:
        combinations += _generate_pair_combinations(
            quoted_survivors, quotes, quantity_needed
        )

    front, dominated_rejections = pareto_front(combinations)

    rejected = hard_filter_rejections + expiry_rejections + dominated_rejections

    return SolverResult(
        computed_at=now,
        component_id=component_id,
        quantity_needed=quantity_needed,
        pareto_set=sorted(front, key=lambda c: c.total_price),
        rejected=rejected,
        quotes_requested=requested,
        quotes_reused=reused,
    )
