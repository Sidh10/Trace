"""STALENESS — ARCHITECTURE.md §4 item 8, §3's back-edge. Serves Recovery &
Replanning (10%, PROJECT.md §2).

Deterministic; no LLM anywhere. This module answers two questions and
nothing else:

  1. **Is this plan still standing on true assumptions?** — by re-reading the
     exact facts the plan depended on and diffing them against what was true
     when it was built.
  2. **If not, what is the EARLIEST stage whose output is now invalid?** — so
     the orchestrator re-enters there rather than from the top.

It does not re-run anything. Executing the re-entry is sequencing, which is
`app/api/routes.py`'s job.

--------------------------------------------------------------------------
A diff against current state, never a timer
--------------------------------------------------------------------------
`capture_preconditions` records the specific field values the plan rests on
at the moment it was built. `detect_staleness` re-reads those same fields now
and reports each one that moved. Nothing here fires on elapsed time alone:
even quote expiry — the one check that mentions the clock — is a comparison
against the quote's OWN `quote_issued_at + quote_valid_hours` (§5.7), which
is a fact about that quote, not a replanning interval. "Replan every N
minutes" would be a timer and an invented threshold both; this is neither.

Each fact maps to the earliest stage its change invalidates:

| Fact that moved                              | Earliest invalid stage |
|----------------------------------------------|------------------------|
| inventory stock / usage / safety levels       | COVERAGE |
| production order deadline / priority / demand | COVERAGE |
| purchase-order status (dependable inbound set)| COVERAGE |
| tracking status of a polled PO                | MONITOR  |
| a new supplier claim on a load-bearing PO     | VERIFY   |
| supplier price / lead time / availability     | SOLVER   |
| supplier reliability / quality / certifications | SOLVER |
| a quote passing `quote_valid_hours`           | SOLVER   |
| the approval threshold                        | RATCHET  |

Two of those deserve their reasoning stated, because they look
interchangeable and are not:

  * **Tracking status changed -> MONITOR, not VERIFY.** MONITOR is what
    physically reads tracking; VERIFY reuses MONITOR's read rather than
    re-probing (item 3's tool-efficiency design). So a tracking change
    invalidates MONITOR's own output first. Re-entering at VERIFY would hand
    it a stale tracking value.
  * **A new supplier claim -> VERIFY, not MONITOR.** MONITOR's tracking read
    is still perfectly valid; what changed is the claim VERIFY compares it
    against. This is the case where re-entry genuinely starts at VERIFY.

--------------------------------------------------------------------------
Rule 5 tagging is what the rollback rule keys off — and it bites
--------------------------------------------------------------------------
`STAGE_REVERSIBILITY` tags each stage per AGENTS.md rule 5. This is not
decoration: **re-entry must never re-run a non-idempotent stage whose own
inputs did not change**, and one stage really is non-idempotent.

`VERIFY` mutates `SupplierRecord.reliability_score` through the
exponentially weighted update. Re-running it against the SAME unchanged
evidence applies the update again — measured, not theorised: SUP-21 goes
0.75 -> 0.45 -> 0.27 -> 0.162 across three passes on identical inputs. A
naive "re-enter at COVERAGE and re-run everything below it" rollback would
therefore silently destroy supplier reliability scores every time an
unrelated stock level moved. That is the bug this tagging exists to prevent,
and `stages_to_rerun` encodes the prevention: a `compensable` stage is
included only when a finding names it directly.

The consequence is deliberate and worth stating plainly: after a
COVERAGE-level re-entry, the prior `VerificationReport` is REUSED. Its
conclusion (a contradiction was found, the score moved) is still true — it
was a fact about a tracking record, and that record has not changed. What
would be false is applying the penalty a second time for the same evidence.

`ERP_WRITE` is tagged `irreversible`, which is why it is not a re-enterable
stage at all. You cannot roll back past it. A plan that was already executed
and then goes stale does not get undone — the replan produces a SUPERSEDING
plan, and the purchase orders the first one created stand (ARCHITECTURE.md
§5 excludes PO cancellation from this system's action space). Those POs are
`pending`, which IS a dependable inbound status, so the next coverage pass
credits them automatically and the superseding plan accounts for stock
already on order rather than double-buying.

--------------------------------------------------------------------------
Bounded, per AGENTS.md rule 6
--------------------------------------------------------------------------
`MAX_REENTRY_PASSES` bounds how many times one call may replan. This is a
termination guarantee ("not an open agent loop", rule 6), not a domain
judgement — it is not a threshold on anything measured. Two passes: one
absorbs the detected change, a second absorbs a change that landed *during*
the first replan. Beyond that the environment is moving faster than planning
converges, which is itself the finding worth surfacing rather than looping
on, so it is reported (`post_replan_verified=False` plus the residual
findings) instead of retried.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.engine.coverage import CoverageReport
from app.engine.monitor import MonitorReport
from app.engine.planner import Plan
from app.engine.solver import SolverResult
from app.environment.clock import clock
from app.environment.schemas import RFQQuote
from app.environment.seed_data import Store

PipelineStage = Literal["COVERAGE", "MONITOR", "VERIFY", "SOLVER", "PLAN", "RATCHET"]

# Earliest first. `reentry_stage` is the minimum over all findings.
STAGE_ORDER: tuple[PipelineStage, ...] = (
    "COVERAGE",
    "MONITOR",
    "VERIFY",
    "SOLVER",
    "PLAN",
    "RATCHET",
)

# AGENTS.md rule 5, applied to pipeline stages rather than plan actions.
# See the module docstring — VERIFY's tag is the one that changes behaviour.
STAGE_REVERSIBILITY: dict[str, str] = {
    "COVERAGE": "idempotent",    # pure read over the store
    "MONITOR": "idempotent",     # pure read; spends tracking calls, changes no state
    "VERIFY": "compensable",     # MUTATES reliability_score — re-running re-applies it
    "SOLVER": "idempotent",      # spends RFQ calls; changes no decision state
    "PLAN": "idempotent",        # pure computation over solver + coverage output
    "RATCHET": "idempotent",     # reads the approval tool; writes nothing
    "ERP_WRITE": "irreversible", # rule 5's one — not a re-enterable stage
}

# Rule 6's termination bound. Not a threshold on anything measured.
MAX_REENTRY_PASSES = 2


def _stage_index(stage: PipelineStage) -> int:
    return STAGE_ORDER.index(stage)


# ==========================================================================
# Shapes
# ==========================================================================


class PreconditionSnapshot(BaseModel):
    """The specific facts a plan depends on, as they were when it was built.

    Every entry is a real field read off a real record — nothing derived,
    nothing scored. This is the assumption ledger made checkable: item 7
    records assumptions as prose for a human, this records them as values a
    diff can test.
    """

    plan_id: str
    component_id: str
    captured_at: datetime

    approval_threshold: float
    inventory: dict[str, float] = Field(default_factory=dict)
    production_orders: dict[str, dict] = Field(default_factory=dict)
    po_states: dict[str, str] = Field(default_factory=dict)
    tracking_states: dict[str, str] = Field(default_factory=dict)
    latest_claim_ids: dict[str, str] = Field(default_factory=dict)
    supplier_states: dict[str, dict] = Field(default_factory=dict)
    quote_expiries: dict[str, datetime] = Field(default_factory=dict)
    chosen_supplier_ids: list[str] = Field(default_factory=list)
    polled_po_ids: list[str] = Field(default_factory=list)


class StalenessFinding(BaseModel):
    """One assumption that no longer holds. `stage` is the EARLIEST pipeline
    stage this particular change invalidates — see the module docstring's
    mapping table."""

    stage: PipelineStage
    kind: str
    subject: str
    was: str
    now: str
    detail: str


class StalenessReport(BaseModel):
    plan_id: str
    component_id: str
    checked_at: datetime
    findings: list[StalenessFinding] = Field(default_factory=list)
    is_stale: bool = False
    reentry_stage: Optional[PipelineStage] = None
    stages_to_rerun: list[PipelineStage] = Field(default_factory=list)
    reentry_note: str = ""

    def findings_at(self, stage: PipelineStage) -> list[StalenessFinding]:
        return [f for f in self.findings if f.stage == stage]


# ==========================================================================
# Capture
# ==========================================================================


def _quote_expiry(quote: RFQQuote) -> datetime:
    """`quote_issued_at + quote_valid_hours` — §5.7's own two fields, nothing
    derived.

    Takes the quote itself. An earlier version reconstructed "the quote the
    solve used" as "the most recent one in `store.rfq_log` for this
    supplier/component", which was correct only because the snapshot ran
    immediately after the solve — an inference resting on an unstated timing
    assumption. `SolverResult.quotes_used` now carries the real objects, so
    this cites rather than infers.
    """
    return quote.quote_issued_at + timedelta(hours=quote.quote_valid_hours)


def capture_preconditions(
    store: Store,
    plan: Plan,
    coverage: CoverageReport,
    monitor: MonitorReport,
    *,
    approval_threshold: float,
    solver_result: Optional[SolverResult] = None,
    now: Optional[datetime] = None,
) -> PreconditionSnapshot:
    """Snapshot what this plan rests on. Call immediately after the plan is
    built, before anything else touches the store.

    `solver_result` supplies `quotes_used` — the actual quotes behind the
    plan. Optional only so a caller holding just a plan can still snapshot
    everything else; without it, quote expiry is simply not checked rather
    than guessed at."""
    now = clock.now() if now is None else now
    component_id = plan.component_id

    inventory_record = store.get_inventory(component_id)
    inventory: dict[str, float] = {}
    if inventory_record is not None:
        inventory = {
            "usable_stock": float(inventory_record.usable_stock),
            "current_stock": float(inventory_record.current_stock),
            "daily_usage": float(inventory_record.daily_usage),
            "safety_stock": float(inventory_record.safety_stock),
            "required_quality_score": float(inventory_record.required_quality_score),
        }

    production_orders = {
        r.production_order_id: {
            "deadline": r.deadline.isoformat(),
            "priority": r.priority,
            "component_required": r.component_required,
        }
        for r in coverage.results
        if r.component_id == component_id
    }

    po_states = {
        po.po_id: po.status
        for po in store.list_purchase_orders(component_id=component_id)
    }

    polled = [d.po_id for d in monitor.polled()]
    tracking_states: dict[str, str] = {}
    latest_claim_ids: dict[str, str] = {}
    for po_id in polled:
        tracking = store.get_tracking(po_id)
        if tracking is not None:
            tracking_states[po_id] = tracking.tracking_status
        inbound = [m for m in store.list_inbox(po_id=po_id) if m.direction == "inbound"]
        if inbound:
            latest_claim_ids[po_id] = inbound[-1].message_id

    chosen_supplier_ids = list(plan.chosen_combination.supplier_ids)
    supplier_states: dict[str, dict] = {}
    quote_expiries: dict[str, datetime] = {}
    for supplier_id in chosen_supplier_ids:
        supplier = store.get_supplier(supplier_id)
        if supplier is None:
            continue
        supplier_states[supplier_id] = {
            "unit_price": supplier.unit_price,
            "lead_time_days": supplier.lead_time_days,
            "available_quantity": supplier.available_quantity,
            "reliability_score": supplier.reliability_score,
            "quality_score": supplier.quality_score,
            "certifications": sorted(supplier.certifications),
        }
        quote = (solver_result.quotes_used or {}).get(supplier_id) if solver_result else None
        if quote is not None:
            quote_expiries[supplier_id] = _quote_expiry(quote)

    return PreconditionSnapshot(
        plan_id=plan.plan_id,
        component_id=component_id,
        captured_at=now,
        approval_threshold=approval_threshold,
        inventory=inventory,
        production_orders=production_orders,
        po_states=po_states,
        tracking_states=tracking_states,
        latest_claim_ids=latest_claim_ids,
        supplier_states=supplier_states,
        quote_expiries=quote_expiries,
        chosen_supplier_ids=chosen_supplier_ids,
        polled_po_ids=polled,
    )


# ==========================================================================
# Detect
# ==========================================================================


def _check_inventory(store: Store, snap: PreconditionSnapshot) -> list[StalenessFinding]:
    record = store.get_inventory(snap.component_id)
    if record is None or not snap.inventory:
        return []
    current = {
        "usable_stock": float(record.usable_stock),
        "current_stock": float(record.current_stock),
        "daily_usage": float(record.daily_usage),
        "safety_stock": float(record.safety_stock),
        "required_quality_score": float(record.required_quality_score),
    }
    return [
        StalenessFinding(
            stage="COVERAGE",
            kind="inventory_changed",
            subject=f"{snap.component_id}.{field}",
            was=str(was),
            now=str(current[field]),
            detail=(
                f"{snap.component_id}'s {field} moved {was} -> {current[field]}; "
                "every coverage figure derived from it is stale."
            ),
        )
        for field, was in snap.inventory.items()
        if current.get(field) != was
    ]


def _check_production_orders(
    store: Store, snap: PreconditionSnapshot
) -> list[StalenessFinding]:
    findings: list[StalenessFinding] = []
    for production_order_id, was in snap.production_orders.items():
        order = store.production_orders.get(production_order_id)
        if order is None:
            findings.append(
                StalenessFinding(
                    stage="COVERAGE",
                    kind="production_order_removed",
                    subject=production_order_id,
                    was="present",
                    now="absent",
                    detail=f"{production_order_id} is no longer in the schedule.",
                )
            )
            continue
        current = {
            "deadline": order.deadline.isoformat(),
            "priority": order.priority,
            "component_required": order.units_planned * order.component_required_per_unit,
        }
        for field, prior in was.items():
            # `deadline` was captured from CoverageResult (a datetime) and is
            # read back from ProductionOrder (a date); compare on the date
            # part so a representation difference is not read as a change.
            if field == "deadline":
                if str(prior)[:10] != str(current[field])[:10]:
                    findings.append(
                        StalenessFinding(
                            stage="COVERAGE",
                            kind="production_deadline_changed",
                            subject=production_order_id,
                            was=str(prior),
                            now=str(current[field]),
                            detail=(
                                f"{production_order_id}'s deadline moved — the "
                                "feasibility verdict that plan was built on no "
                                "longer applies."
                            ),
                        )
                    )
                continue
            if current[field] != prior:
                findings.append(
                    StalenessFinding(
                        stage="COVERAGE",
                        kind=f"production_{field}_changed",
                        subject=production_order_id,
                        was=str(prior),
                        now=str(current[field]),
                        detail=(
                            f"{production_order_id}'s {field} moved "
                            f"{prior} -> {current[field]}."
                        ),
                    )
                )
    return findings


def _check_po_states(store: Store, snap: PreconditionSnapshot) -> list[StalenessFinding]:
    """PO status drives `dependable_inbound`, which drives coverage — so a
    status change invalidates COVERAGE, the earliest stage."""
    findings: list[StalenessFinding] = []
    current = {
        po.po_id: po.status
        for po in store.list_purchase_orders(component_id=snap.component_id)
    }
    for po_id, was in snap.po_states.items():
        now_status = current.get(po_id, "absent")
        if now_status != was:
            findings.append(
                StalenessFinding(
                    stage="COVERAGE",
                    kind="po_status_changed",
                    subject=po_id,
                    was=was,
                    now=now_status,
                    detail=(
                        f"{po_id} moved {was} -> {now_status}; whether its "
                        "quantity still counts as dependable inbound has changed."
                    ),
                )
            )
    for po_id in current:
        if po_id not in snap.po_states:
            findings.append(
                StalenessFinding(
                    stage="COVERAGE",
                    kind="po_appeared",
                    subject=po_id,
                    was="absent",
                    now=current[po_id],
                    detail=(
                        f"{po_id} did not exist when the plan was built; coverage "
                        "has not accounted for it."
                    ),
                )
            )
    return findings


def _check_tracking(store: Store, snap: PreconditionSnapshot) -> list[StalenessFinding]:
    """MONITOR, not VERIFY — MONITOR is what physically reads tracking, and
    VERIFY reuses that read. See the module docstring."""
    findings: list[StalenessFinding] = []
    for po_id, was in snap.tracking_states.items():
        tracking = store.get_tracking(po_id)
        now_status = tracking.tracking_status if tracking is not None else "absent"
        if now_status != was:
            findings.append(
                StalenessFinding(
                    stage="MONITOR",
                    kind="tracking_status_changed",
                    subject=po_id,
                    was=was,
                    now=now_status,
                    detail=(
                        f"{po_id}'s tracking moved {was} -> {now_status}; MONITOR's "
                        "own read is stale, so anything downstream that reused it is too."
                    ),
                )
            )
    return findings


def _check_claims(store: Store, snap: PreconditionSnapshot) -> list[StalenessFinding]:
    """VERIFY — a new claim arrived. MONITOR's tracking read is still valid;
    what changed is the assertion VERIFY compares it against."""
    findings: list[StalenessFinding] = []
    for po_id in snap.polled_po_ids:
        inbound = [m for m in store.list_inbox(po_id=po_id) if m.direction == "inbound"]
        now_id = inbound[-1].message_id if inbound else "none"
        was = snap.latest_claim_ids.get(po_id, "none")
        if now_id != was:
            findings.append(
                StalenessFinding(
                    stage="VERIFY",
                    kind="new_supplier_claim",
                    subject=po_id,
                    was=was,
                    now=now_id,
                    detail=(
                        f"A newer supplier claim ({now_id}) exists for {po_id}; the "
                        "verified contradiction was checked against "
                        f"{was}."
                    ),
                )
            )
    return findings


def _check_suppliers(store: Store, snap: PreconditionSnapshot) -> list[StalenessFinding]:
    findings: list[StalenessFinding] = []
    for supplier_id, was in snap.supplier_states.items():
        supplier = store.get_supplier(supplier_id)
        if supplier is None:
            findings.append(
                StalenessFinding(
                    stage="SOLVER",
                    kind="supplier_removed",
                    subject=supplier_id,
                    was="present",
                    now="absent",
                    detail=f"{supplier_id} is no longer in the catalog.",
                )
            )
            continue
        current = {
            "unit_price": supplier.unit_price,
            "lead_time_days": supplier.lead_time_days,
            "available_quantity": supplier.available_quantity,
            "reliability_score": supplier.reliability_score,
            "quality_score": supplier.quality_score,
            "certifications": sorted(supplier.certifications),
        }
        for field, prior in was.items():
            if current[field] != prior:
                findings.append(
                    StalenessFinding(
                        stage="SOLVER",
                        kind=f"supplier_{field}_changed",
                        subject=supplier_id,
                        was=str(prior),
                        now=str(current[field]),
                        detail=(
                            f"{supplier_id}'s {field} moved {prior} -> "
                            f"{current[field]}; the Pareto set was computed on the "
                            "old value."
                        ),
                    )
                )
    return findings


def _check_quote_expiry(
    snap: PreconditionSnapshot, now: datetime
) -> list[StalenessFinding]:
    """§5.7's `quote_valid_hours`, enforced as a real constraint rather than
    displayed. A comparison against the quote's own issue time — a fact about
    that quote, not a replanning interval."""
    return [
        StalenessFinding(
            stage="SOLVER",
            kind="quote_expired",
            subject=supplier_id,
            was=f"valid until {expiry.isoformat()}",
            now=f"now {now.isoformat()}",
            detail=(
                f"{supplier_id}'s quote passed its quote_valid_hours window at "
                f"{expiry.isoformat()}. The price and lead time this plan was "
                "built on are no longer quoted."
            ),
        )
        for supplier_id, expiry in snap.quote_expiries.items()
        if now > expiry
    ]


def _check_approval_threshold(
    snap: PreconditionSnapshot, current_threshold: float
) -> list[StalenessFinding]:
    if current_threshold == snap.approval_threshold:
        return []
    return [
        StalenessFinding(
            stage="RATCHET",
            kind="approval_threshold_changed",
            subject="approval_threshold",
            was=str(snap.approval_threshold),
            now=str(current_threshold),
            detail=(
                "The autonomous purchase threshold moved; the execute-or-escalate "
                "verdict was decided against the old one."
            ),
        )
    ]


def _plan_reentry(findings: list[StalenessFinding]) -> tuple[Optional[PipelineStage], list[PipelineStage], str]:
    """Earliest-conflict rollback, with the rule-5 guard.

    `reentry_stage` is the earliest stage any finding invalidates.
    `stages_to_rerun` is that stage onward MINUS any non-idempotent stage no
    finding names directly — see the module docstring for why re-running
    VERIFY gratuitously corrupts reliability scores.
    """
    if not findings:
        return None, [], "No assumption changed; the plan still stands."

    earliest = min(findings, key=lambda f: _stage_index(f.stage)).stage
    named = {f.stage for f in findings}

    to_rerun: list[PipelineStage] = []
    skipped: list[str] = []
    for stage in STAGE_ORDER[_stage_index(earliest):]:
        if STAGE_REVERSIBILITY[stage] != "idempotent" and stage not in named:
            skipped.append(stage)
            continue
        to_rerun.append(stage)

    note = (
        f"Earliest invalidated stage: {earliest} "
        f"({', '.join(sorted(f.kind for f in findings if f.stage == earliest))}). "
        f"Re-running {' -> '.join(to_rerun)}."
    )
    if skipped:
        note += (
            f" Skipping {', '.join(skipped)}: tagged "
            f"{STAGE_REVERSIBILITY[skipped[0]]} under AGENTS.md rule 5 and no "
            "finding names it, so re-running would re-apply a side effect "
            "(the reliability update) for evidence that has not changed."
        )
    return earliest, to_rerun, note


def detect_staleness(
    store: Store,
    snapshot: PreconditionSnapshot,
    *,
    current_approval_threshold: float,
    now: Optional[datetime] = None,
) -> StalenessReport:
    """Re-read every fact in `snapshot` and report the ones that moved."""
    now = clock.now() if now is None else now

    findings: list[StalenessFinding] = [
        *_check_inventory(store, snapshot),
        *_check_production_orders(store, snapshot),
        *_check_po_states(store, snapshot),
        *_check_tracking(store, snapshot),
        *_check_claims(store, snapshot),
        *_check_suppliers(store, snapshot),
        *_check_quote_expiry(snapshot, now),
        *_check_approval_threshold(snapshot, current_approval_threshold),
    ]

    reentry_stage, stages_to_rerun, note = _plan_reentry(findings)
    return StalenessReport(
        plan_id=snapshot.plan_id,
        component_id=snapshot.component_id,
        checked_at=now,
        findings=findings,
        is_stale=bool(findings),
        reentry_stage=reentry_stage,
        stages_to_rerun=stages_to_rerun,
        reentry_note=note,
    )
