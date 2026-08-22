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

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.audit.provenance import ProvenanceGraph, build_provenance_graph
from app.engine.coverage import compute_coverage
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import Plan, run_planner
from app.engine.ratchet import DecisionBrief, run_ratchet
from app.engine.solver import run_solver
from app.engine.verify import run_verification_cycle
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

    def erp_write_count(self) -> int:
        return len(self.erp_writes)


# ==========================================================================
# State — the two idempotency registries (see the module docstring)
# ==========================================================================

_RUNS_BY_COMPONENT: dict[str, PipelineRun] = {}
_ERP_WRITES_BY_PLAN: dict[str, list[ERPUpdateResponse]] = {}
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
    _run_seq = 0


def _store() -> Store:
    """Resolved at call time, not import time, so a test can swap
    `seed_data.STATE` and have the orchestrator see it."""
    return seed_data.STATE


# ==========================================================================
# THE ERP WRITE — the only call to POST /erp/update in this codebase
# ==========================================================================


def _write_erp_once(store: Store, plan: Plan, trigger: str) -> list[ERPUpdateResponse]:
    """AGENTS.md rule 5's one irreversible action, behind its one guard.

    Idempotent per `plan_id`: a plan already written returns its existing
    response(s) untouched. This is what makes a double approval, or an
    approval arriving after an execute, produce one write rather than two.

    The action is `store_plan` (§5.9) — recording the decided plan. It is
    deliberately the ONLY action emitted: translating each `purchase_split`
    into a `create_alternate_po` would require the orchestrator to choose an
    `expected_delivery`, a PO id, and an approval ceiling per split, which is
    plan-shaped logic this module is explicitly not allowed to hold. Logged
    in OPEN_ITEMS.md as needing sign-off, not decided unilaterally here.
    """
    existing = _ERP_WRITES_BY_PLAN.get(plan.plan_id)
    if existing is not None:
        return existing

    response = store.update_erp(
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
    writes = [response]
    _ERP_WRITES_BY_PLAN[plan.plan_id] = writes
    return writes


# ==========================================================================
# The assembled pipeline
# ==========================================================================


def run_pipeline(
    store: Optional[Store] = None,
    *,
    component_id: str,
    now: Optional[datetime] = None,
) -> PipelineRun:
    """Walk the full chain for one component and return the run.

    Idempotent per component: a component already handled returns its
    existing run rather than replanning (see the module docstring). No stage
    below contains a decision — each line is a call into the module that owns
    that judgement.
    """
    store = _store() if store is None else store
    now = clock.now() if now is None else now

    cached = _RUNS_BY_COMPONENT.get(component_id)
    if cached is not None:
        return cached

    stages: list[str] = []

    # COVERAGE — before MONITOR, because MONITOR's gate is its output.
    coverage = compute_coverage(store, now=now)
    stages.append("COVERAGE")

    # MONITOR — gated on coverage.polling_targets(); the TOOL GATE lives here.
    monitor = run_monitor_cycle(store, coverage=coverage, now=now)
    stages.append("MONITOR (tool gate: load-bearing POs only)")

    # VERIFY — same gate, reusing MONITOR's tracking read.
    verification = run_verification_cycle(
        store, coverage=coverage, monitor=monitor, now=now
    )
    stages.append("VERIFY")

    # HARD FILTER + SOLVER — hard_filter runs inside run_solver, before RFQ.
    coverage_results = [r for r in coverage.results if r.component_id == component_id]
    quantity_needed = sum(r.component_required for r in coverage_results)
    solver_result = run_solver(
        store, component_id=component_id, quantity_needed=quantity_needed, now=now
    )
    stages.append("HARD FILTER + SOLVER")

    # PLAN — may be None when nothing survived; run_ratchet handles that.
    plan = run_planner(store, component_id, solver_result, coverage_results, now=now)
    stages.append("PLAN")

    # RATCHET — the execute-or-escalate decision. Not this module's call.
    brief = run_ratchet(store, plan, solver_result, now=now)
    stages.append("RATCHET")

    # ERP WRITE — if and only if the verdict is execute.
    erp_writes: list[ERPUpdateResponse] = []
    awaiting_approval = False
    if brief.decision == "execute" and plan is not None:
        erp_writes = _write_erp_once(store, plan, trigger="ratchet_verdict_execute")
        stages.append("ERP WRITE")
    else:
        awaiting_approval = True
        stages.append("ERP WRITE SKIPPED (escalate — awaiting human approval)")

    # AUDIT — the provenance graph over everything above.
    graph = build_provenance_graph(
        coverage=coverage,
        monitor=monitor,
        verification=verification,
        solver_result=solver_result,
        plan=plan,
        brief=brief,
        now=now,
    )
    stages.append("AUDIT")

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
    )
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
