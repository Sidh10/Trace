"""ORCHESTRATOR — ARCHITECTURE.md §3's control flow, assembled. The endpoints
the frontend (items 11/12) and the judge panel call.

This module sequences and guards. It does not decide. Every judgement in the
chain below already belongs to an engine module that owns it, and this file
calls those functions in order without re-implementing, re-deriving, or
second-guessing any of them. If you find yourself adding a threshold, a
comparison, or a "should we..." here, it belongs upstream instead.

--------------------------------------------------------------------------
THE ERP-WRITE BOUNDARY — the reason this module exists
--------------------------------------------------------------------------
AGENTS.md rule 5: "Exactly one action is irreversible: POST /erp/update."
`_write_erp_once` below is the ONLY place in this entire codebase that calls
it. Three guarantees, each with a test that fails if it breaks:

1. **The write fires if and only if RATCHET's verdict is `execute`.** An
   `escalate` verdict performs zero writes — not even `record_escalation`,
   which is itself one of §5.9's six actions and therefore itself a write.
   The pipeline stops at the `DecisionBrief` and waits.
2. **`escalate` waits for a human.** `POST /agent/approval/{plan_id}` is the
   separate step: approving resumes the pipeline and performs the write;
   rejecting closes the plan with no write, recorded on the run.
3. **The write is idempotent per `plan_id`.** `_ERP_WRITES_BY_PLAN` is
   consulted before every write and returned instead of writing twice. Two
   approvals of the same plan, or an approval after an execute, produce one
   write total.

A second, coarser idempotency exists at the run level (`_RUNS_BY_COMPONENT`):
re-handling the same component returns the run already computed rather than
building a fresh plan with a fresh `plan_id`. Without it, "fire the same
disruption twice" would mint PLAN-0001 and PLAN-0002 and the per-plan_id
write guard would correctly let both through — two writes for one disruption.
Both guards are needed; neither substitutes for the other. **Item 8
(staleness) is what will legitimately invalidate this cache** — a genuinely
new disruption on an already-handled component must force a replan, and that
re-entry logic is item 8's, not this module's. `reset_orchestrator_state()`
is the hook until then.

--------------------------------------------------------------------------
Stage order, and one honest deviation from the §3 diagram
--------------------------------------------------------------------------
§3 draws `MONITOR -> COVERAGE -> TOOL GATE -> VERIFY -> HARD FILTER ->
SOLVER -> PLAN -> RATCHET -> ERP WRITE -> AUDIT`. What actually runs:

  COVERAGE -> MONITOR -> VERIFY -> SOLVER(+HARD FILTER) -> PLAN -> RATCHET
  -> [ERP WRITE iff execute] -> AUDIT

Two differences, both real and neither a shortcut:

  * **COVERAGE runs before MONITOR.** MONITOR is drawn first because it is
    what *initiates* the loop, but its gate is `coverage.polling_targets()` —
    it cannot know which POs are load-bearing until coverage has said so.
    `monitor.run_monitor_cycle` takes a `CoverageReport` for exactly this
    reason and its own docstring says so. Running coverage first is the data
    dependency, not a reordering of the logic.
  * **TOOL GATE is not a separate call.** It is already embodied in the two
    places that spend calls: MONITOR gates on `polling_targets()`, and VERIFY
    gates on the same set and reuses MONITOR's tracking read rather than
    re-probing. Adding a third "gate" stage here would be a stage that gates
    nothing. The counts it would have reported are in
    `ProvenanceGraph.tool_calls`.

`PipelineRun.stages` records the sequence actually walked, so the audit trail
shows the real order rather than the diagram's.

--------------------------------------------------------------------------
The one derivation this module performs
--------------------------------------------------------------------------
`quantity_needed = sum(r.component_required for r in <this component's
coverage results>)` — the total demand across every production order drawing
on the component. `run_solver` takes this as a caller-supplied input by
design (its docstring: "turning a coverage shortfall into a specific unit
quantity is a planning decision"), so *someone* must supply it and the
orchestrator is the caller. It is a sum of a field COVERAGE already computed
(`units_planned x component_required_per_unit`), not an invented threshold or
metric. Flagged in OPEN_ITEMS.md as the single derivation here, so it is
visible rather than buried.

--------------------------------------------------------------------------
What this module deliberately does NOT do
--------------------------------------------------------------------------
It does not mark a PO delayed when VERIFY confirms a contradiction. That is
tempting — the contradiction is proven, and `mark_po_delayed` is a §5.9
action — but it is (a) an ERP write, which must not happen on the escalate
path, and (b) exactly the question OPEN_ITEMS.md logs for item 8 ("should
COVERAGE stop crediting a contradicted PO"). The pipeline responds to whatever
environment state exists; changing that state on its own initiative is not
sequencing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.audit.provenance import (
    ProvenanceGraph,
    append_erp_write_edges,
    build_provenance_graph,
)
from app.engine.baseline import (
    BaselineComparisonReport,
    PipelineVariant,
    run_baseline_comparison,
)
from app.engine.tool_audit import build_tool_audit
from app.engine.contingency import (
    ContingencyPlan,
    FiredContingency,
    evaluate_contingencies,
    fallback_solver_result,
    register_contingencies,
    reset_contingency_sequence,
    to_staleness_report,
)
from app.engine.coverage import CoverageReport, compute_coverage
from app.engine.monitor import MonitorReport, run_monitor_cycle
from app.engine.planner import Plan, find_disrupted_po, run_planner
from app.engine.ratchet import DecisionBrief, run_ratchet
from app.engine.solver import SolverResult, run_solver
from app.engine.staleness import STAGE_ORDER as _STALENESS_STAGE_ORDER
from app.engine.staleness import (
    MAX_REENTRY_PASSES,
    PipelineStage,
    PreconditionSnapshot,
    StalenessReport,
    capture_preconditions,
    detect_staleness,
)
from app.engine.verify import VerificationReport, run_verification_cycle
from app.environment import seed_data
from app.environment.clock import clock
from app.environment.schemas import ERPUpdateRequest, ERPUpdateResponse
from app.environment.seed_data import Store

router = APIRouter()


# ==========================================================================
# Request / response shapes
# ==========================================================================


class HandleEventRequest(BaseModel):
    """Either name the component directly, or name a PO and let the
    orchestrator resolve its component. Nothing else is accepted — the
    pipeline reads the rest from environment state."""

    component_id: Optional[str] = None
    po_id: Optional[str] = None


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    approved_by: str = "operator"
    note: str = ""


class PipelineRun(BaseModel):
    """One assembled pass. Carries the two things the caller asked for (the
    `DecisionBrief` and the `ProvenanceGraph`) plus the ERP-boundary state,
    so whether a write happened — and why or why not — is inspectable rather
    than inferred."""

    run_id: str
    component_id: str
    started_at: datetime
    stages: list[str]

    decision: Literal["execute", "escalate"]
    plan_id: Optional[str]
    brief: DecisionBrief
    graph: ProvenanceGraph

    # The ERP boundary, made visible.
    erp_writes: list[ERPUpdateResponse] = Field(default_factory=list)
    awaiting_approval: bool = False
    approval_outcome: Optional[Literal["approved", "rejected"]] = None
    approval_note: str = ""

    # Item 8. Populated only when this run replaced a stale one.
    staleness: Optional[StalenessReport] = None
    reentered_at_stage: Optional[str] = None
    superseded_plan_id: Optional[str] = None
    post_replan_verified: Optional[bool] = None
    residual_staleness: Optional[StalenessReport] = None

    # Item 9. `contingencies` is what was pre-committed for THIS plan;
    # `fired_contingencies` is what fired to produce it.
    contingencies: list[ContingencyPlan] = Field(default_factory=list)
    fired_contingencies: list[FiredContingency] = Field(default_factory=list)

    def erp_write_count(self) -> int:
        return len(self.erp_writes)


class _StageOutputs(BaseModel):
    """Every stage's output for one component, retained so a re-entry can
    reuse the stages that are still valid instead of recomputing from the
    top. Kept off `PipelineRun` deliberately — this is orchestrator working
    memory, not part of the HTTP response, and embedding six full reports in
    every response would bloat it for no caller benefit."""

    model_config = {"arbitrary_types_allowed": True}

    coverage: CoverageReport
    monitor: MonitorReport
    verification: VerificationReport
    solver_result: SolverResult
    plan: Optional[Plan]
    brief: DecisionBrief
    snapshot: Optional[PreconditionSnapshot] = None
    # Item 9: fallbacks pre-committed at PLAN time for this plan.
    contingencies: list[ContingencyPlan] = Field(default_factory=list)


# ==========================================================================
# State — the two idempotency registries (see the module docstring)
# ==========================================================================

_RUNS_BY_COMPONENT: dict[str, PipelineRun] = {}
_ERP_WRITES_BY_PLAN: dict[str, list[ERPUpdateResponse]] = {}
# Item 8: the stage outputs a re-entry reuses. Same key as the run cache.
_OUTPUTS_BY_COMPONENT: dict[str, _StageOutputs] = {}
_run_seq = 0


def _next_run_id() -> str:
    global _run_seq
    _run_seq += 1
    return f"RUN-{_run_seq:04d}"


def reset_orchestrator_state() -> None:
    """Test hook, and the reset the judge panel (item 11) needs between
    injections — the orchestrator's own memory has to clear alongside
    `build_store()`'s clean slate (ARCHITECTURE.md §8), or a second demo run
    replays the first one's cached decision. Matches the established
    `reset_event_sequence()` / `reset_plan_sequence()` /
    `reset_provenance_sequences()` pattern."""
    global _run_seq
    _RUNS_BY_COMPONENT.clear()
    _ERP_WRITES_BY_PLAN.clear()
    _OUTPUTS_BY_COMPONENT.clear()
    reset_contingency_sequence()
    _run_seq = 0


def _store() -> Store:
    """Resolved at call time, not import time, so a test can swap
    `seed_data.STATE` and have the orchestrator see it."""
    return seed_data.STATE


def _disrupted_po_id(store: Store, component_id: str) -> Optional[str]:
    """The PO id whose own `approval_required_above` governs this
    component's decision, when one exists. One canonical resolution
    (`planner.find_disrupted_po`), reused everywhere a po_id is needed for
    this component: SOLVER's budget hard-filter, RATCHET's cost check, and
    the staleness threshold comparison below — never re-derived per call
    site, so all three can never disagree about which PO that is."""
    po = find_disrupted_po(store, component_id)
    return po.po_id if po is not None else None


def _current_approval_threshold(store: Store, component_id: str) -> float:
    """Read live off the disrupted PO's own `approval_required_above` when
    one exists, else `app.config.TRACE_APPROVAL_THRESHOLD` — the SAME
    resolution `Store.resolve_approval_threshold` gives `check_approval` and
    RATCHET's displayed threshold, so the staleness check and the decision
    it is checking always see the same value, whether that value comes from
    a PO's own field or the global default — and a test that moves either
    one is detectable as the RATCHET-level change it is."""
    return store.resolve_approval_threshold(_disrupted_po_id(store, component_id))


# Stage order for re-entry, mirroring staleness.STAGE_ORDER. Imported rather
# than redefined would be cleaner, but the tuple is needed as a lookup here
# and as a Literal there; this keeps one source of truth for the ORDER.
STAGE_ORDER_ALL: tuple[PipelineStage, ...] = _STALENESS_STAGE_ORDER
_STAGE_INDEX: dict[PipelineStage, int] = {
    stage: index for index, stage in enumerate(STAGE_ORDER_ALL)
}


# ==========================================================================
# THE ERP WRITE — the only call to POST /erp/update in this codebase
# ==========================================================================


def _write_erp_once(
    store: Store, plan: Plan, trigger: str, *, now: Optional[datetime] = None
) -> list[ERPUpdateResponse]:
    """AGENTS.md rule 5's one irreversible action, behind its one guard.

    Idempotent per `plan_id`: a plan already written returns its existing
    responses untouched. This is what makes a double approval, or an approval
    arriving after an execute, produce one write SET rather than two.

    Two actions are emitted, in this order (§5.9):

      1. `store_plan` — the decision record, written FIRST so that if PO
         creation were to fail partway, the ERP still shows what was
         intended.
      2. one `create_alternate_po` per `purchase_split` action — the actual
         procurement. Signed off; previously withheld pending that.

    `expected_delivery` is the sim clock's current time plus THAT supplier's
    own `lead_time_days`, both already on the `PurchaseSplitAction`. No
    approval ceiling is passed: RATCHET already authorised the plan's total
    cost, so a per-PO ceiling would be a second, redundant gate on a decision
    already made (the schema's own default stands).

    **PO ids are not generated here.** Omitting `po_id` from the payload
    hands generation to `Store.update_erp`'s existing anchor-safe range
    (item 1) — the same code path that already guarantees no collision with
    a reserved anchor id. Hand-rolling a range here is exactly the bug class
    ARCHITECTURE.md §8 records as already hit and fixed once.
    """
    existing = _ERP_WRITES_BY_PLAN.get(plan.plan_id)
    if existing is not None:
        return existing

    now = clock.now() if now is None else now
    writes: list[ERPUpdateResponse] = [
        store.update_erp(
            ERPUpdateRequest(
                action="store_plan",
                production_order_id=None,
                payload={
                    "plan_id": plan.plan_id,
                    "component_id": plan.component_id,
                    "total_cost": plan.total_cost,
                    "chosen_combination": plan.chosen_combination.label,
                    "deadline_feasible": plan.deadline_feasible,
                    "actions": [a.model_dump(mode="json") for a in plan.actions],
                    "written_because": trigger,
                },
            )
        )
    ]

    for split in plan.purchase_actions():
        writes.append(
            store.update_erp(
                ERPUpdateRequest(
                    action="create_alternate_po",
                    payload={
                        # No "po_id" — Store generates it, anchor-safe.
                        "component_id": plan.component_id,
                        "supplier_id": split.supplier_id,
                        "quantity": split.qty,
                        "unit_price": split.unit_price,
                        "expected_delivery": (
                            now + timedelta(days=split.lead_time_days)
                        ).date().isoformat(),
                    },
                )
            )
        )

    _ERP_WRITES_BY_PLAN[plan.plan_id] = writes
    return writes


# ==========================================================================
# The assembled pipeline
# ==========================================================================


def _walk(
    store: Store,
    component_id: str,
    now: datetime,
    *,
    from_stage: PipelineStage = "COVERAGE",
    stages_to_rerun: Optional[list[PipelineStage]] = None,
    prior: Optional[_StageOutputs] = None,
    variant: PipelineVariant = "trace",
) -> tuple[_StageOutputs, list[str]]:
    """Run the chain from `from_stage` onward, reusing `prior` for everything
    earlier — and for any stage the rollback rule says to SKIP.

    `stages_to_rerun` comes from `staleness._plan_reentry`; it is the suffix
    minus non-idempotent stages no finding named. A stage not in that list is
    reused from `prior`, never recomputed. That is the whole point of
    earliest-conflict re-entry: recomputing a stage whose inputs did not
    change is at best wasted tool calls and at worst — for VERIFY — a
    duplicate reliability penalty for evidence that has not changed.

    Contains no decisions. Which stages to run was decided by `staleness.py`;
    this only executes that decision in order.
    """
    walked: list[str] = []
    run_set = set(stages_to_rerun) if stages_to_rerun is not None else set(STAGE_ORDER_ALL)
    start = _STAGE_INDEX[from_stage]

    def should_run(stage: PipelineStage) -> bool:
        return _STAGE_INDEX[stage] >= start and stage in run_set

    def reuse(stage: PipelineStage, label: str) -> None:
        walked.append(f"{label} (reused — not invalidated)")

    # COVERAGE — before MONITOR, because MONITOR's gate is its output.
    if should_run("COVERAGE") or prior is None:
        coverage = compute_coverage(store, now=now)
        walked.append("COVERAGE")
    else:
        coverage = prior.coverage
        reuse("COVERAGE", "COVERAGE")

    # MONITOR — gated on coverage.polling_targets(); the TOOL GATE lives here.
    if should_run("MONITOR") or prior is None:
        monitor = run_monitor_cycle(store, coverage=coverage, now=now)
        walked.append("MONITOR (tool gate: load-bearing POs only)")
    else:
        monitor = prior.monitor
        reuse("MONITOR", "MONITOR")

    # VERIFY — same gate, reusing MONITOR's tracking read. NON-IDEMPOTENT:
    # it applies the exponentially weighted reliability update.
    # SKIPPED under cheapest_always and claim_ablation variants.
    if variant in ("cheapest_always", "claim_ablation"):
        verification = VerificationReport(
            verified_at=now,
            verifications=[],
            skipped=[],
            reliability_changes=[],
            probes_made=0,
            probes_reused_from_monitor=0,
        )
        walked.append("VERIFY (skipped by variant config)")
    elif should_run("VERIFY") or prior is None:
        verification = run_verification_cycle(
            store, coverage=coverage, monitor=monitor, now=now
        )
        walked.append("VERIFY")
    else:
        verification = prior.verification
        walked.append(
            "VERIFY (reused — compensable under rule 5; re-running would "
            "re-apply the reliability update for unchanged evidence)"
        )

    coverage_results = [r for r in coverage.results if r.component_id == component_id]

    # The PO this disruption is about, if any — one resolution, reused by
    # both the hard filter's budget check and RATCHET's cost check below, so
    # neither can silently disagree with the other about which PO's own
    # `approval_required_above` (or the global default, absent one) governs
    # this pass. Computed unconditionally: RATCHET may run even when SOLVER
    # is reused from `prior` (e.g. an approval-threshold-only staleness
    # finding), and needs the same po_id either way.
    po_id = _disrupted_po_id(store, component_id)

    # HARD FILTER + SOLVER — hard_filter runs inside run_solver, before RFQ.
    if should_run("SOLVER") or prior is None:
        quantity_needed = sum(r.component_required for r in coverage_results)
        solver_result = run_solver(
            store,
            component_id=component_id,
            quantity_needed=quantity_needed,
            po_id=po_id,
            now=now,
            min_price_only=(variant == "cheapest_always"),
            skip_quality_filter=(variant == "cheapest_always"),
        )
        if variant == "cheapest_always":
            walked.append("SOLVER (cheapest-always: min price, no quality filter)")
        else:
            walked.append("HARD FILTER + SOLVER")
    else:
        solver_result = prior.solver_result
        reuse("SOLVER", "HARD FILTER + SOLVER")

    # PLAN — may be None when nothing survived; run_ratchet handles that.
    if should_run("PLAN") or prior is None:
        plan = run_planner(
            store,
            component_id,
            solver_result,
            coverage_results,
            now=now,
            allow_reschedule=(variant != "cheapest_always"),
        )
        if variant == "cheapest_always":
            walked.append("PLAN (cheapest-always: no production reschedule)")
        else:
            walked.append("PLAN")
    else:
        plan = prior.plan
        reuse("PLAN", "PLAN")

    # RATCHET — the execute-or-escalate decision. Not this module's call.
    if should_run("RATCHET") or prior is None:
        brief = run_ratchet(store, plan, solver_result, po_id=po_id, now=now)
        walked.append("RATCHET")
    else:
        brief = prior.brief
        reuse("RATCHET", "RATCHET")

    return (
        _StageOutputs(
            coverage=coverage,
            monitor=monitor,
            verification=verification,
            solver_result=solver_result,
            plan=plan,
            brief=brief,
        ),
        walked,
    )


def _finish(
    store: Store,
    component_id: str,
    outputs: _StageOutputs,
    stages: list[str],
    now: datetime,
    *,
    staleness: Optional[StalenessReport] = None,
    reentered_at_stage: Optional[str] = None,
    superseded_plan_id: Optional[str] = None,
    fired_contingencies: Optional[list[FiredContingency]] = None,
    variant: PipelineVariant = "trace",
) -> PipelineRun:
    """ERP WRITE (iff execute) then AUDIT, then cache the run and its stage
    outputs. Shared by the first pass and every re-entry so the boundary is
    enforced identically on both."""
    plan, brief = outputs.plan, outputs.brief

    erp_writes: list[ERPUpdateResponse] = []
    awaiting_approval = False
    if brief.decision == "execute" and plan is not None:
        erp_writes = _write_erp_once(
            store, plan, trigger="ratchet_verdict_execute", now=now
        )
        stages.append("ERP WRITE")
    else:
        awaiting_approval = True
        stages.append("ERP WRITE SKIPPED (escalate — awaiting human approval)")

    # Item 10: per-call precondition log, built before the graph so it can
    # be embedded alongside item 7's counters.
    tool_audit_report = build_tool_audit(
        monitor=outputs.monitor,
        verification=outputs.verification,
        solver_result=outputs.solver_result,
        brief=brief,
        now=now,
    )

    graph = build_provenance_graph(
        coverage=outputs.coverage,
        monitor=outputs.monitor,
        verification=outputs.verification,
        solver_result=outputs.solver_result,
        plan=plan,
        brief=brief,
        erp_writes=erp_writes,
        fired_contingencies=fired_contingencies or [],
        tool_audit=tool_audit_report,
        now=now,
    )
    stages.append("AUDIT")

    if plan is not None:
        outputs.snapshot = capture_preconditions(
            store,
            plan,
            outputs.coverage,
            outputs.monitor,
            approval_threshold=_current_approval_threshold(store, component_id),
            solver_result=outputs.solver_result,
            now=now,
        )
        # Item 9: pre-commit the fallbacks NOW, at plan time. Registering
        # them later would make them reactions, not contingencies.
        outputs.contingencies = register_contingencies(
            plan, outputs.solver_result, now=now
        )

    run = PipelineRun(
        run_id=_next_run_id(),
        component_id=component_id,
        started_at=now,
        stages=stages,
        decision=brief.decision,
        plan_id=plan.plan_id if plan is not None else None,
        brief=brief,
        graph=graph,
        erp_writes=erp_writes,
        awaiting_approval=awaiting_approval,
        staleness=staleness,
        reentered_at_stage=reentered_at_stage,
        superseded_plan_id=superseded_plan_id,
        contingencies=outputs.contingencies,
        fired_contingencies=fired_contingencies or [],
    )
    cache_key = component_id if variant == "trace" else f"{component_id}:{variant}"
    _RUNS_BY_COMPONENT[cache_key] = run
    _OUTPUTS_BY_COMPONENT[cache_key] = outputs
    # Also index by component_id if no default run exists yet
    if component_id not in _RUNS_BY_COMPONENT:
        _RUNS_BY_COMPONENT[component_id] = run
        _OUTPUTS_BY_COMPONENT[component_id] = outputs

    return run


def run_pipeline(
    store: Optional[Store] = None,
    *,
    component_id: str,
    now: Optional[datetime] = None,
    variant: PipelineVariant = "trace",
) -> PipelineRun:
    """Walk the chain for one component and return the run.

    **Item 8 changed the cache rule.** A cached run is returned only if its
    plan's preconditions still hold. If `staleness.detect_staleness` finds an
    assumption that moved, the run is NOT returned — the pipeline re-enters at
    the earliest invalidated stage, produces a superseding plan, and then
    verifies THAT plan's own preconditions before treating it as final.

    Before item 8 this cache was keyed on component_id with no invalidation,
    so a genuinely new disruption on an already-handled component wrongly
    replayed the stale decision. That was logged as item 8's to fix; this is
    the fix.
    """
    store = _store() if store is None else store
    now = clock.now() if now is None else now

    cache_key = component_id if variant == "trace" else f"{component_id}:{variant}"
    cached = _RUNS_BY_COMPONENT.get(cache_key)
    prior = _OUTPUTS_BY_COMPONENT.get(cache_key)

    if cached is not None:
        if prior is None or prior.snapshot is None:
            return cached  # nothing to check against (no plan was produced)

        if variant in ("static_workflow", "cheapest_always", "retry_only"):
            # Fixed stage sequence: staleness detector (item 8) and contingency re-entry (item 9) disabled
            return cached

        # Item 9 is checked FIRST, and the order is deliberate. A fired
        # contingency and generic staleness can both be true of the same
        # change — a supplier's lead time rising is both "a supplier field
        # moved" (SOLVER-level staleness, re-solve) and "the contingency we
        # pre-committed for exactly this" (PLAN-level, use the fallback).
        # The pre-commitment is the cheaper and more faithful answer: it
        # spends no RFQ call and honours a decision already made and
        # recorded. Falling through to a fresh solve would silently discard
        # the contingency and make registering it pointless.
        fired = evaluate_contingencies(store, prior.contingencies, now=now)
        if fired:
            return _reenter_on_contingency(store, component_id, cached, prior, fired, now)

        report = detect_staleness(
            store,
            prior.snapshot,
            current_approval_threshold=_current_approval_threshold(store, component_id),
            now=now,
        )
        if not report.is_stale:
            return cached
        return _reenter(store, component_id, cached, prior, report, now)

    outputs, stages = _walk(store, component_id, now, variant=variant)
    return _finish(store, component_id, outputs, stages, now, variant=variant)


def _reenter_on_contingency(
    store: Store,
    component_id: str,
    stale_run: PipelineRun,
    prior: _StageOutputs,
    fired: list[FiredContingency],
    now: datetime,
) -> PipelineRun:
    """Item 9's re-entry — item 8's machinery, landing on the fallback.

    `to_staleness_report` renders the fired triggers as item 8's own
    `StalenessReport` (reentry_stage=PLAN, stages_to_rerun=[PLAN, RATCHET]),
    and `fallback_solver_result` hands `run_planner` a Pareto set containing
    exactly the pre-committed fallback. So PLANNER is unmodified: the
    fallback still passes the same deadline-feasibility filter, the same
    allocation, the same reschedule logic. A contingency changes WHICH
    combination is planned, never HOW.

    SOLVER is skipped, which is the point — the alternative was already on
    the Pareto set at plan time, so rediscovering it would spend RFQ calls
    for an answer already known.
    """
    report = to_staleness_report(fired, now=now)
    assert report is not None  # unreachable with a non-empty `fired`

    stages = [
        f"CONTINGENCY FIRED ({len(fired)}) — {report.reentry_note}",
    ]
    for firing in fired:
        trigger = firing.contingency.failure_trigger
        stages.append(
            f"  trigger '{trigger.kind}' on {trigger.subject}: {firing.detail}"
        )
    if stale_run.erp_writes:
        stages.append(
            f"NOTE: {stale_run.plan_id} was already executed "
            f"({len(stale_run.erp_writes)} irreversible write(s)). Rule 5 — the "
            "primary is no longer compensable, so this SUPERSEDES rather than "
            "substitutes; the POs it created stand."
        )

    outputs, walked = _walk(
        store,
        component_id,
        now,
        from_stage="PLAN",
        stages_to_rerun=report.stages_to_rerun,
        prior=_StageOutputs(
            coverage=prior.coverage,
            monitor=prior.monitor,
            verification=prior.verification,
            # The injected fallback-only Pareto set — this is what makes
            # run_planner select the pre-committed alternative unchanged.
            solver_result=fallback_solver_result(prior.solver_result, fired),
            plan=prior.plan,
            brief=prior.brief,
        ),
    )
    stages.extend(walked)

    # POST-REPLAN VERIFICATION — a fallback is not exempt. It was chosen
    # earlier, which says nothing about whether it still holds now.
    verified = True
    residual: Optional[StalenessReport] = None
    if outputs.plan is not None:
        fresh = capture_preconditions(
            store,
            outputs.plan,
            outputs.coverage,
            outputs.monitor,
            approval_threshold=_current_approval_threshold(store, component_id),
            solver_result=outputs.solver_result,
            now=now,
        )
        residual = detect_staleness(
            store,
            fresh,
            current_approval_threshold=_current_approval_threshold(store, component_id),
            now=now,
        )
        if residual.is_stale:
            verified = False
            stages.append(
                "POST-REPLAN VERIFICATION FAILED — the pre-committed fallback "
                f"does not hold either: {residual.reentry_note}"
            )
        else:
            stages.append(
                "POST-REPLAN VERIFICATION: the fallback's preconditions hold"
            )
            residual = None

    run = _finish(
        store,
        component_id,
        outputs,
        stages,
        now,
        staleness=report,
        reentered_at_stage=report.reentry_stage,
        superseded_plan_id=stale_run.plan_id,
        fired_contingencies=fired,
    )
    run.post_replan_verified = verified
    run.residual_staleness = residual
    _RUNS_BY_COMPONENT[component_id] = run
    return run


def _reenter(
    store: Store,
    component_id: str,
    stale_run: PipelineRun,
    prior: _StageOutputs,
    report: StalenessReport,
    now: datetime,
) -> PipelineRun:
    """Earliest-conflict rollback, then post-replan verification.

    Bounded by `MAX_REENTRY_PASSES` (AGENTS.md rule 6 — a termination
    guarantee, not a domain threshold). After each replan the NEW plan's own
    preconditions are captured and re-checked: a second pass is not trusted
    just for being second. If it is still stale when the bound is reached,
    that is reported (`post_replan_verified=False` plus the residual
    findings) rather than looped on — an environment moving faster than
    planning converges is a finding, not something to retry silently.
    """
    stages = [
        f"STALENESS DETECTED ({len(report.findings)} finding(s)) — {report.reentry_note}"
    ]
    if stale_run.erp_writes:
        stages.append(
            f"NOTE: {stale_run.plan_id} was already executed "
            f"({len(stale_run.erp_writes)} irreversible write(s)). Rule 5 — those "
            "stand; this replan SUPERSEDES rather than undoes. The POs it created "
            "are `pending`, so the new coverage pass credits them."
        )

    current_report = report
    outputs = prior
    residual: Optional[StalenessReport] = None
    verified = False

    for _pass in range(MAX_REENTRY_PASSES):
        outputs, walked = _walk(
            store,
            component_id,
            now,
            from_stage=current_report.reentry_stage or "COVERAGE",
            stages_to_rerun=current_report.stages_to_rerun,
            prior=outputs,
        )
        stages.extend(walked)

        # POST-REPLAN VERIFICATION — the new plan is not final until its own
        # preconditions are confirmed to hold.
        if outputs.plan is None:
            stages.append("POST-REPLAN VERIFICATION: no plan produced; nothing to verify")
            verified = True
            residual = None
            break

        fresh_snapshot = capture_preconditions(
            store,
            outputs.plan,
            outputs.coverage,
            outputs.monitor,
            approval_threshold=_current_approval_threshold(store, component_id),
            solver_result=outputs.solver_result,
            now=now,
        )
        residual = detect_staleness(
            store,
            fresh_snapshot,
            current_approval_threshold=_current_approval_threshold(store, component_id),
            now=now,
        )
        if not residual.is_stale:
            stages.append("POST-REPLAN VERIFICATION: new plan's preconditions hold")
            verified = True
            residual = None
            break

        stages.append(
            f"POST-REPLAN VERIFICATION FAILED — {len(residual.findings)} assumption(s) "
            f"already moved again: {residual.reentry_note}"
        )
        current_report = residual

    if not verified:
        stages.append(
            f"RE-ENTRY BOUND REACHED ({MAX_REENTRY_PASSES} passes, AGENTS.md rule 6). "
            "Reporting residual staleness rather than looping — the environment is "
            "changing faster than planning converges, which is itself the finding."
        )

    run = _finish(
        store,
        component_id,
        outputs,
        stages,
        now,
        staleness=report,
        reentered_at_stage=report.reentry_stage,
        superseded_plan_id=stale_run.plan_id,
    )
    run.post_replan_verified = verified
    run.residual_staleness = residual
    _RUNS_BY_COMPONENT[component_id] = run
    return run


def resolve_approval(
    store: Optional[Store] = None,
    *,
    plan_id: str,
    approved: bool,
    approved_by: str = "operator",
    note: str = "",
) -> PipelineRun:
    """The separate human step an `escalate` verdict waits for.

    Approving performs the write that RATCHET withheld — through the same
    `_write_erp_once` guard, so approving twice writes once. Rejecting closes
    the plan with no write at all, recorded on the run.
    """
    store = _store() if store is None else store

    run = next(
        (r for r in _RUNS_BY_COMPONENT.values() if r.plan_id == plan_id), None
    )
    if run is None:
        raise KeyError(f"unknown plan_id: {plan_id}")
    if run.brief.chosen_plan is None:
        raise ValueError(
            f"{plan_id} has no plan to approve — the solver produced no "
            "feasible candidate, so there is nothing to execute."
        )

    if approved:
        run.erp_writes = _write_erp_once(
            store, run.brief.chosen_plan, trigger=f"human_approval_by:{approved_by}"
        )
        # The write happened after this run's graph was built, so the trail
        # gets the edges that only became true now. Appending is idempotent
        # and leaves every earlier edge untouched.
        append_erp_write_edges(run.graph, run.brief.chosen_plan, run.erp_writes)
        run.approval_outcome = "approved"
    else:
        # No write, by construction — the rejection is recorded on the run,
        # not in the ERP. `record_escalation` would itself be a write.
        run.approval_outcome = "rejected"

    run.awaiting_approval = False
    run.approval_note = note
    return run


# ==========================================================================
# HTTP surface
# ==========================================================================


@router.post("/agent/handle-event", response_model=PipelineRun)
def handle_event(req: HandleEventRequest) -> PipelineRun:
    """Walk the full chain and return the DecisionBrief and ProvenanceGraph.

    Supply `component_id`, or `po_id` to resolve the component from it. With
    neither, MONITOR's own detection picks the component: the first
    load-bearing PO whose tracking contradicts its PO record.
    """
    store = _store()

    component_id = req.component_id
    if component_id is None and req.po_id is not None:
        po = store.get_purchase_order(req.po_id)
        if po is None:
            raise HTTPException(status_code=404, detail=f"unknown po_id: {req.po_id}")
        component_id = po.component_id

    if component_id is None:
        # No target named — let MONITOR detect one. Nothing is decided here:
        # the component is read off whatever event MONITOR already emitted.
        coverage = compute_coverage(store)
        monitor = run_monitor_cycle(store, coverage=coverage)
        detected = next((e for e in monitor.events if e.component_id), None)
        if detected is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "no disruption detected — MONITOR polled every load-bearing "
                    "PO and found no contradiction. Name a component_id or "
                    "po_id explicitly to plan anyway."
                ),
            )
        component_id = detected.component_id

    if store.get_inventory(component_id) is None:
        raise HTTPException(
            status_code=404, detail=f"unknown component_id: {component_id}"
        )

    return run_pipeline(store, component_id=component_id)


@router.post("/agent/approval/{plan_id}", response_model=PipelineRun)
def approve_plan(plan_id: str, req: ApprovalDecisionRequest) -> PipelineRun:
    """Resolve an escalated plan.

    Namespaced under `/agent/` rather than the bare `/approval/{plan_id}`
    deliberately: the environment already serves the problem statement's own
    `POST /approval/check` (§5.8), and a bare `/approval/{plan_id}` would sit
    in the same path space as a spec endpoint — a route-ordering accident
    waiting to happen, and confusing besides. Flagged in OPEN_ITEMS.md.
    """
    try:
        return resolve_approval(
            _store(),
            plan_id=plan_id,
            approved=req.approved,
            approved_by=req.approved_by,
            note=req.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agent/runs/{component_id}", response_model=PipelineRun)
def get_run(component_id: str) -> PipelineRun:
    run = _RUNS_BY_COMPONENT.get(component_id)
    if run is None:
        raise HTTPException(
            status_code=404, detail=f"no pipeline run for component: {component_id}"
        )
    return run


@router.post("/agent/reset")
def reset_agent() -> dict:
    """Clear the orchestrator's run cache and write registry. The judge panel
    (item 11) needs this between injections — see
    `reset_orchestrator_state()`. Does not touch environment state; that is
    `build_store()`'s job."""
    reset_orchestrator_state()
    return {"status": "reset"}


# ==========================================================================
# Item 13 — Multi-baseline comparison harness (ARCHITECTURE.md §11)
# ==========================================================================


@router.post("/baseline/compare/{scenario_name}", response_model=BaselineComparisonReport)
@router.post("/baseline/compare", response_model=BaselineComparisonReport)
def compare_baselines(
    scenario_name: str = "po_7712_delay",
    component_id: Optional[str] = Query(default="COMP-104"),
) -> BaselineComparisonReport:
    """Run all four pipeline variants (plus bonus claim_ablation) against a
    disruption scenario and return their outcomes, spend, hidden tests, and
    silent failures together.
    """
    comp_id = component_id or "COMP-104"
    return run_baseline_comparison(scenario_name=scenario_name, component_id=comp_id)
