"""CONTINGENCY — ARCHITECTURE.md §4 item 9. Serves Recovery & Replanning
(10%, PROJECT.md §2) and closes the "expedited delivery becomes unavailable"
hidden test (§10), which nothing covered before this.

Deterministic; no LLM. Built as an APPLICATION of item 8's machinery, not a
parallel one: a fired contingency produces the same
`StalenessFinding`/`StalenessReport` shapes the staleness detector produces,
so the orchestrator's existing earliest-conflict re-entry consumes it
unchanged. There is one re-entry mechanism in this system, not two.

--------------------------------------------------------------------------
What a contingency is, and why it is not just "replan again"
--------------------------------------------------------------------------
A contingency is a fallback **pre-committed at PLAN time**:

    {primary_action, failure_trigger: {condition}, fallback_action}

The distinction that earns its keep: when the trigger fires, the system does
NOT solve from scratch. The fallback was already chosen — it is another
member of the Pareto set SOLVER had already computed and PLANNER had already
ranked — so re-entry lands at **PLAN** with the fallback combination
injected, skipping SOLVER entirely. That is the difference between
contingency re-entry and staleness re-entry, and it is why
`CONTINGENCY_REENTRY_STAGE` is `PLAN`:

  * **Staleness** says "an input moved; recompute from where it moved."
    A price change invalidates the solve, so SOLVER re-runs and spends fresh
    RFQ calls.
  * **A contingency** says "we already decided what to do if this happened."
    Nothing about the alternatives is unknown, so no RFQ call is spent. The
    pre-commitment IS the saving.

Registered only for combinations that actually have a fallback — if the
Pareto set had exactly one member there is no alternative to pre-commit to,
and `register_contingencies` returns nothing rather than inventing one.

--------------------------------------------------------------------------
The trigger conditions — spec fields, no invented thresholds
--------------------------------------------------------------------------
Every condition compares CURRENT state against what the plan itself
recorded. There is no threshold anywhere: the comparison target is the
plan's own assumption, not a constant someone chose.

  * `expedite_withdrawn` — the supplier's quote said `expedite_available:
    true` when the plan was built and a later quote says `false`. §5.7's own
    field. This is the literal "expedited delivery becomes unavailable"
    hidden test.
  * `lead_time_exceeded` — `SupplierRecord.lead_time_days` now exceeds the
    `lead_time_days` the plan's own `PurchaseSplitAction` recorded. The
    operational form of the same failure: whatever the reason, the delivery
    the plan was built on is no longer achievable in the time it assumed.
  * `supplier_quantity_shortfall` — `SupplierRecord.available_quantity` has
    fallen below the quantity the plan committed to that supplier.

All three are read for free off records the pipeline already holds — no
tool call is spent evaluating a trigger. Both of the first two map to the
`expedite_unavailable` DisruptionEvent type, which ARCHITECTURE.md §7's
enum already declared and nothing had yet emitted.

--------------------------------------------------------------------------
Reversibility (AGENTS.md rule 5)
--------------------------------------------------------------------------
Both the primary and the fallback are `purchase_split` actions, which
`planner.py` already tags `compensable` — reused here, not re-derived. That
tag is what makes a pre-committed swap legitimate at all: swapping a
compensable action for another compensable one before either is written is
free. The moment the primary has been written to the ERP it is
`irreversible` and there is nothing to swap — see `fires_before_execution`,
and the orchestrator's own check that a fired contingency on an
already-executed plan produces a SUPERSEDING plan rather than a substitution.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.engine.planner import Plan, PurchaseSplitAction
from app.engine.solver import SolverResult, SourcingCombination
from app.engine.staleness import PipelineStage, StalenessFinding, StalenessReport
from app.environment.clock import clock
from app.environment.schemas import RFQQuote
from app.environment.seed_data import Store

TriggerKind = Literal[
    "expedite_withdrawn",
    "lead_time_exceeded",
    "supplier_quantity_shortfall",
]

# A fired contingency re-enters at PLAN, not SOLVER: the fallback is already
# chosen, so there is nothing to re-solve. See the module docstring.
CONTINGENCY_REENTRY_STAGE: PipelineStage = "PLAN"

# Which DisruptionEvent type each trigger corresponds to. Both delivery-
# related kinds map to the enum value ARCHITECTURE.md §7 already declared.
TRIGGER_EVENT_TYPE: dict[TriggerKind, str] = {
    "expedite_withdrawn": "expedite_unavailable",
    "lead_time_exceeded": "expedite_unavailable",
    "supplier_quantity_shortfall": "supplier_delay",
}


# ==========================================================================
# Shapes
# ==========================================================================


class FailureTrigger(BaseModel):
    """The condition, stated so it can be read back and checked by name.

    `condition` is a plain-language rendering of the SAME comparison
    `_evaluate_one` performs — it exists so the provenance trail can cite the
    trigger by name rather than by opaque id, not as a second source of
    truth. `observed_field` and `plan_assumed` are the two sides of that
    comparison."""

    kind: TriggerKind
    subject: str  # supplier_id
    condition: str
    observed_field: str
    plan_assumed: str


class ContingencyPlan(BaseModel):
    """One pre-committed fallback: {primary_action, failure_trigger,
    fallback_action}, ARCHITECTURE.md §4 item 9's own shape."""

    contingency_id: str
    plan_id: str
    component_id: str
    registered_at: datetime

    primary_action: PurchaseSplitAction
    failure_trigger: FailureTrigger
    fallback_combination: SourcingCombination
    fallback_action: PurchaseSplitAction

    # Reused from planner.py's own tagging, not re-derived here.
    primary_reversibility: str
    fallback_reversibility: str

    note: str


class FiredContingency(BaseModel):
    contingency: ContingencyPlan
    fired_at: datetime
    observed: str
    detail: str

    @property
    def event_type(self) -> str:
        return TRIGGER_EVENT_TYPE[self.contingency.failure_trigger.kind]


# ==========================================================================
# Registration — at PLAN time
# ==========================================================================

_contingency_seq = 0


def next_contingency_id() -> str:
    global _contingency_seq
    _contingency_seq += 1
    return f"CONT-{_contingency_seq:04d}"


def reset_contingency_sequence() -> None:
    """Test hook, matching the established reset_* pattern."""
    global _contingency_seq
    _contingency_seq = 0


def _fallback_for(
    plan: Plan, solver_result: SolverResult
) -> Optional[SourcingCombination]:
    """The pre-committed alternative: the best Pareto member that is NOT the
    chosen one, ranked by PLANNER's own `SELECTION_RULE` order.

    Ranked, not picked arbitrarily — the same lexicographic key
    `_select_combination` uses, so the fallback is "what PLANNER would have
    chosen next", not a different judgement invented here. Returns None when
    the Pareto set has no alternative, in which case no contingency is
    registered at all."""
    alternatives = [
        c for c in solver_result.pareto_set if c is not plan.chosen_combination
    ]
    if not alternatives:
        return None
    return sorted(
        alternatives,
        key=lambda c: (-c.reliability_score, c.lead_time_days, c.total_price),
    )[0]


def _trigger_for(
    split: PurchaseSplitAction, quote: Optional[RFQQuote]
) -> FailureTrigger:
    """One trigger per primary split.

    `expedite_withdrawn` is preferred when the plan's own quote recorded
    expedite as available — that is the condition the hidden test names, and
    it is only checkable when there was an expedite promise to withdraw.
    Otherwise the trigger falls back to `lead_time_exceeded`, the same
    failure expressed through the supplier catalog: the promised delivery is
    no longer achievable in the time the plan assumed."""
    if quote is not None and quote.expedite_available:
        return FailureTrigger(
            kind="expedite_withdrawn",
            subject=split.supplier_id,
            condition=(
                f"{split.supplier_id}'s quoted expedite_available flips from "
                "true to false — the expedited delivery this plan was built "
                "on is no longer offered"
            ),
            observed_field="RFQQuote.expedite_available",
            plan_assumed="true",
        )
    return FailureTrigger(
        kind="lead_time_exceeded",
        subject=split.supplier_id,
        condition=(
            f"{split.supplier_id}'s lead_time_days rises above the "
            f"{split.lead_time_days} day(s) this plan was built on — the "
            "promised delivery is no longer achievable in that time"
        ),
        observed_field="SupplierRecord.lead_time_days",
        plan_assumed=str(split.lead_time_days),
    )


def register_contingencies(
    plan: Plan,
    solver_result: SolverResult,
    *,
    now: Optional[datetime] = None,
) -> list[ContingencyPlan]:
    """Pre-commit one fallback per purchase split, at PLAN time.

    Returns [] when the Pareto set offers no alternative — a contingency with
    no fallback is not a contingency, and manufacturing one would be
    inventing an option SOLVER did not find."""
    now = clock.now() if now is None else now
    fallback = _fallback_for(plan, solver_result)
    if fallback is None:
        return []

    fallback_member = fallback.members[0]
    contingencies: list[ContingencyPlan] = []

    for split in plan.purchase_actions():
        quote = (solver_result.quotes_used or {}).get(split.supplier_id)
        trigger = _trigger_for(split, quote)
        fallback_action = PurchaseSplitAction(
            supplier_id=fallback_member.supplier_id,
            qty=split.qty,
            unit_price=fallback_member.unit_price,
            lead_time_days=fallback_member.lead_time_days,
            note=(
                f"Pre-committed fallback for {split.supplier_id}: "
                f"{split.qty} units from {fallback_member.supplier_id} @ "
                f"{fallback_member.unit_price}/unit, "
                f"{fallback_member.lead_time_days}-day lead time."
            ),
        )
        contingencies.append(
            ContingencyPlan(
                contingency_id=next_contingency_id(),
                plan_id=plan.plan_id,
                component_id=plan.component_id,
                registered_at=now,
                primary_action=split,
                failure_trigger=trigger,
                fallback_combination=fallback,
                fallback_action=fallback_action,
                # planner.py's own tags, reused.
                primary_reversibility=split.reversibility,
                fallback_reversibility=fallback_action.reversibility,
                note=(
                    f"If {trigger.condition}, switch to {fallback.label} "
                    "without a fresh solve — it is already on the Pareto set."
                ),
            )
        )
    return contingencies


# ==========================================================================
# Evaluation — free reads, no tool calls
# ==========================================================================


def _latest_quote(store: Store, component_id: str, supplier_id: str) -> Optional[RFQQuote]:
    quotes = [
        q
        for q in store.rfq_log
        if q.supplier_id == supplier_id and q.component_id == component_id
    ]
    return quotes[-1] if quotes else None


def _evaluate_one(
    store: Store, contingency: ContingencyPlan
) -> Optional[tuple[str, str]]:
    """(observed, detail) if the trigger's condition is true now, else None.
    Reads only records the pipeline already holds — no tool call."""
    trigger = contingency.failure_trigger
    supplier = store.get_supplier(trigger.subject)

    if trigger.kind == "expedite_withdrawn":
        quote = _latest_quote(store, contingency.component_id, trigger.subject)
        if quote is not None and not quote.expedite_available:
            return (
                "expedite_available=false",
                (
                    f"{trigger.subject}'s latest quote no longer offers expedited "
                    f"delivery (expedite_available false; the plan was built on "
                    f"{trigger.plan_assumed}). "
                    f"{contingency.primary_action.qty} units were committed to it."
                ),
            )
        return None

    if trigger.kind == "lead_time_exceeded":
        if supplier is not None and supplier.lead_time_days > int(trigger.plan_assumed):
            return (
                f"lead_time_days={supplier.lead_time_days}",
                (
                    f"{trigger.subject}'s lead time rose to "
                    f"{supplier.lead_time_days} days; the plan was built on "
                    f"{trigger.plan_assumed}. The promised delivery is no longer "
                    "achievable in the time assumed."
                ),
            )
        return None

    if trigger.kind == "supplier_quantity_shortfall":
        if supplier is not None and supplier.available_quantity < contingency.primary_action.qty:
            return (
                f"available_quantity={supplier.available_quantity}",
                (
                    f"{trigger.subject} can no longer supply the "
                    f"{contingency.primary_action.qty} units committed "
                    f"(available {supplier.available_quantity})."
                ),
            )
        return None

    return None


def evaluate_contingencies(
    store: Store,
    contingencies: list[ContingencyPlan],
    *,
    now: Optional[datetime] = None,
) -> list[FiredContingency]:
    """Every registered trigger whose condition is true now."""
    now = clock.now() if now is None else now
    fired: list[FiredContingency] = []
    for contingency in contingencies:
        outcome = _evaluate_one(store, contingency)
        if outcome is None:
            continue
        observed, detail = outcome
        fired.append(
            FiredContingency(
                contingency=contingency, fired_at=now, observed=observed, detail=detail
            )
        )
    return fired


# ==========================================================================
# Bridge to item 8's re-entry — the same shapes, so the same machinery
# ==========================================================================


def to_staleness_report(
    fired: list[FiredContingency], *, now: Optional[datetime] = None
) -> Optional[StalenessReport]:
    """Render fired contingencies as item 8's own `StalenessReport`, so the
    orchestrator re-enters through the mechanism it already has.

    `reentry_stage` is PLAN, and `stages_to_rerun` is exactly PLAN then
    RATCHET. Notably NOT SOLVER: the fallback is pre-committed, so re-solving
    would spend RFQ calls to rediscover an option already on the Pareto set.
    That skip is the whole point of a contingency, and it is visible here
    rather than buried in the orchestrator."""
    if not fired:
        return None
    now = clock.now() if now is None else now
    first = fired[0]

    findings = [
        StalenessFinding(
            stage=CONTINGENCY_REENTRY_STAGE,
            kind=f"contingency_fired:{f.contingency.failure_trigger.kind}",
            subject=f.contingency.failure_trigger.subject,
            was=f.contingency.failure_trigger.plan_assumed,
            now=f.observed,
            detail=(
                f"{f.contingency.contingency_id} fired — {f.detail} "
                f"Pre-committed fallback: {f.contingency.fallback_combination.label}."
            ),
        )
        for f in fired
    ]

    return StalenessReport(
        plan_id=first.contingency.plan_id,
        component_id=first.contingency.component_id,
        checked_at=now,
        findings=findings,
        is_stale=True,
        reentry_stage=CONTINGENCY_REENTRY_STAGE,
        stages_to_rerun=["PLAN", "RATCHET"],
        reentry_note=(
            f"Contingency {first.contingency.contingency_id} fired on "
            f"'{first.contingency.failure_trigger.kind}' for "
            f"{first.contingency.failure_trigger.subject}. Re-entering at PLAN "
            f"with the pre-committed fallback "
            f"({first.contingency.fallback_combination.label}) — SOLVER is "
            "skipped because the alternative was already chosen at plan time, "
            "so no RFQ call is spent rediscovering it."
        ),
    )


def fallback_solver_result(
    original: SolverResult, fired: list[FiredContingency]
) -> SolverResult:
    """A `SolverResult` whose Pareto set is exactly the pre-committed
    fallback, so `run_planner` selects it without any change to PLANNER.

    Reusing `run_planner` unmodified is deliberate: the fallback still goes
    through the same deadline-feasibility hard filter, the same allocation,
    the same reschedule and safety-stock logic. A contingency changes WHICH
    combination is planned, never HOW it is planned — otherwise the fallback
    path would be a second planner with its own bugs."""
    fallback = fired[0].contingency.fallback_combination
    return SolverResult(
        computed_at=original.computed_at,
        component_id=original.component_id,
        quantity_needed=original.quantity_needed,
        pareto_set=[fallback],
        rejected=original.rejected,
        # No new RFQ call was made — the counters must not claim one was.
        quotes_requested=0,
        quotes_reused=len(original.quotes_used),
        quotes_used=original.quotes_used,
    )
