"""The simulated environment's REST surface — ARCHITECTURE.md §6.

Endpoints marked "extension" below are not in the problem statement's §16
suggested-API list, but implement a data source or capability the spec
names explicitly elsewhere (cited per endpoint). Nothing here invents a new
concept — see AGENTS.md rule 8 and ARCHITECTURE.md §5 for the boundary this
file is required to respect. Every write funnels through Store (seed_data.py);
this module is a thin HTTP layer, no business logic.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.environment.clock import clock
from app.environment.schemas import (
    ApprovalCheckRequest,
    ApprovalCheckResponse,
    ERPUpdateRequest,
    ERPUpdateResponse,
    InventoryRecord,
    ProductionOrder,
    PurchaseOrder,
    RFQQuote,
    RFQRequest,
    SupplierMessage,
    SupplierMessageRequest,
    SupplierMessageResponse,
    SupplierRecord,
    TrackingRecord,
)
# Imported as a MODULE, not `from ... import STATE`, so the name resolves at
# call time. A direct name binding here would freeze this router to whichever
# Store existed at import — and the orchestrator (app/api/routes.py) resolves
# it dynamically, so the two could end up serving DIFFERENT Store instances:
# a disruption injected through these endpoints would be invisible to the
# agent planning against the other one. That is a demo-killer for the judge
# panel (item 11), which injects here and reads results there. Found while
# smoke-testing the orchestrator end to end.
from app.environment import seed_data

router = APIRouter()


# --------------------------------------------------------------------------
# §5.1 Inventory Database
# --------------------------------------------------------------------------


@router.get("/inventory", response_model=list[InventoryRecord])
def list_inventory() -> list[InventoryRecord]:
    return seed_data.STATE.list_inventory()


@router.get("/inventory/{component_id}", response_model=InventoryRecord)
def get_inventory(component_id: str) -> InventoryRecord:
    record = seed_data.STATE.get_inventory(component_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown component_id: {component_id}")
    return record


# --------------------------------------------------------------------------
# §5.2 Purchase Order System
# --------------------------------------------------------------------------


@router.get("/purchase-orders", response_model=list[PurchaseOrder])
def list_purchase_orders(
    status: Optional[str] = Query(default=None),
    component_id: Optional[str] = Query(default=None),
) -> list[PurchaseOrder]:
    return seed_data.STATE.list_purchase_orders(status=status, component_id=component_id)


@router.get("/purchase-orders/{po_id}", response_model=PurchaseOrder)
def get_purchase_order(po_id: str) -> PurchaseOrder:
    record = seed_data.STATE.get_purchase_order(po_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown po_id: {po_id}")
    return record


# --------------------------------------------------------------------------
# §5.3 Supplier Catalog
# --------------------------------------------------------------------------


@router.get("/suppliers", response_model=list[SupplierRecord])
def list_suppliers(component_id: Optional[str] = Query(default=None)) -> list[SupplierRecord]:
    return seed_data.STATE.list_suppliers(component_id=component_id)


# --------------------------------------------------------------------------
# §5.6 Supplier Communication Tool
# --------------------------------------------------------------------------


@router.post("/suppliers/{supplier_id}/message", response_model=SupplierMessageResponse)
def message_supplier(supplier_id: str, req: SupplierMessageRequest) -> SupplierMessageResponse:
    if supplier_id not in seed_data.STATE.suppliers:
        raise HTTPException(status_code=404, detail=f"unknown supplier_id: {supplier_id}")
    return seed_data.STATE.send_supplier_message(supplier_id, req.to, req.subject, req.body)


# --------------------------------------------------------------------------
# §5.5 Simulated Email Inbox — EXTENSION.
# The spec names this as a data source the agent monitors (§4.1 "supplier
# messages") but §16's suggested-API list omits a GET for it. Exposing the
# inbox the spec already describes is not a new feature.
# --------------------------------------------------------------------------


@router.get("/inbox", response_model=list[SupplierMessage])
def list_inbox(
    supplier_id: Optional[str] = Query(default=None),
    po_id: Optional[str] = Query(default=None),
) -> list[SupplierMessage]:
    return seed_data.STATE.list_inbox(supplier_id=supplier_id, po_id=po_id)


@router.get("/inbox/{message_id}", response_model=SupplierMessage)
def get_inbox_message(message_id: str) -> SupplierMessage:
    message = seed_data.STATE.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail=f"unknown message_id: {message_id}")
    return message


# --------------------------------------------------------------------------
# §5.7 RFQ Tool
# --------------------------------------------------------------------------


@router.post("/rfq", response_model=list[RFQQuote])
def request_rfq(req: RFQRequest) -> list[RFQQuote]:
    if req.component_id not in seed_data.STATE.inventory:
        raise HTTPException(status_code=404, detail=f"unknown component_id: {req.component_id}")
    return seed_data.STATE.request_rfq(req)


# --------------------------------------------------------------------------
# §5.8 Budget and Approval Tool
# --------------------------------------------------------------------------


@router.post("/approval/check", response_model=ApprovalCheckResponse)
def check_approval(req: ApprovalCheckRequest) -> ApprovalCheckResponse:
    return seed_data.STATE.check_approval(req)


# --------------------------------------------------------------------------
# §5.9 ERP Update Tool — the one irreversible action, AGENTS.md rule 5
# --------------------------------------------------------------------------


@router.post("/erp/update", response_model=ERPUpdateResponse)
def update_erp(req: ERPUpdateRequest) -> ERPUpdateResponse:
    try:
        return seed_data.STATE.update_erp(req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------
# §5.10 Tracking or Verification Tool
# --------------------------------------------------------------------------


@router.get("/tracking/{po_id}", response_model=TrackingRecord)
def get_tracking(po_id: str) -> TrackingRecord:
    record = seed_data.STATE.get_tracking(po_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown po_id: {po_id}")
    return record


# --------------------------------------------------------------------------
# §5.4 Production Schedule
# --------------------------------------------------------------------------


@router.get("/production-schedule", response_model=list[ProductionOrder])
def get_production_schedule() -> list[ProductionOrder]:
    return seed_data.STATE.list_production_schedule()


# --------------------------------------------------------------------------
# Simulated clock — EXTENSION. ARCHITECTURE.md §5 explicitly permits "the
# simulated clock and time pressure" as part of the environment; this is the
# clock's own HTTP surface, needed by anything that has to move sim time
# forward (item 2b's polling, item 8's staleness re-entry, item 11's judge
# panel later). No disruption-injection or scenario logic lives here.
# --------------------------------------------------------------------------


@router.get("/clock")
def get_clock() -> dict:
    return {"now": clock.iso()}


@router.post("/clock/advance")
def advance_clock(hours: float = 0.0, days: float = 0.0) -> dict:
    new_time = clock.advance(hours=hours, days=days)
    return {"now": new_time.isoformat()}


# --------------------------------------------------------------------------
# Disruption Injection & Simulator Reset — Items 11/12 (Judge Panel)
# --------------------------------------------------------------------------


@router.post("/environment/reset")
def reset_environment() -> dict:
    """Reset the simulated environment to a clean initial state (build_store())
    and clear the orchestrator's state and caches."""
    from datetime import datetime, timezone
    from app import config
    config.TRACE_APPROVAL_THRESHOLD = 150000.0
    seed_data.STATE = seed_data.build_store()
    from app.api.routes import reset_orchestrator_state
    reset_orchestrator_state()
    return {"status": "reset", "message": "Environment reset to clean initial state."}


def _mark_po_delayed_and_sync_threshold(store: "seed_data.Store", po_id: str) -> None:
    """Set a PO's status to `delayed`, and — the reason this exists rather
    than a bare assignment — sync its own `approval_required_above` DOWN if
    a lower global override is ALREADY active.

    Why: once a PO is delayed, `planner.find_disrupted_po` names it "the
    disrupted PO" for its component, and RATCHET/SOLVER start reading ITS
    OWN threshold (`Store.resolve_approval_threshold`) instead of the global
    `config.TRACE_APPROVAL_THRESHOLD` (this session's real per-PO §5.2
    threading). `exceeds_approval`'s own handler already syncs every
    CURRENTLY-delayed PO down when IT runs — that alone only covers
    override-then-disrupt... no, disrupt-then-override. It does NOT cover
    the reverse order (`exceeds_approval` first, a delay-marking scenario
    second): a PO delayed AFTER the override was already active would still
    carry its old, unchanged, higher field, silently un-overriding an
    already-active global policy change. Confirmed live in the browser —
    `exceeds_approval -> stale_erp` reverted from ESCALATE/50000 back to
    EXECUTE/150000. Calling this at the delay-marking site closes that
    direction; the two together make the sync order-independent."""
    po = store.purchase_orders.get(po_id)
    if po is None:
        return
    po.status = "delayed"
    from app import config

    if config.TRACE_APPROVAL_THRESHOLD < po.approval_required_above:
        po.approval_required_above = config.TRACE_APPROVAL_THRESHOLD


@router.post("/environment/inject/{scenario_name}")
def inject_scenario(scenario_name: str) -> dict:
    """Inject a hidden-test disruption scenario into the simulated environment.
    Reuses exact setup logic from test_coverage, test_verify, test_solver,
    test_staleness, test_contingency."""
    from datetime import timedelta
    store = seed_data.STATE

    if scenario_name == "supplier_delay":
        store.send_supplier_message(
            supplier_id="SUP-21",
            to="supplier21@example.com",
            subject="Status check on PO-7712",
            body="Any update on PO-7712?",
        )
        _mark_po_delayed_and_sync_threshold(store, "PO-7712")
        return {
            "scenario": "supplier_delay",
            "target_component": "COMP-104",
            "target_po": "PO-7712",
            "detail": "PO-7712 marked delayed after tracking contradiction; SUP-21 message logged.",
        }

    elif scenario_name == "quality_fail":
        if "SUP-18" in store.suppliers:
            store.suppliers["SUP-18"].quality_score = 0.71
        if "COMP-104" in store.inventory:
            store.inventory["COMP-104"].required_quality_score = 0.85
        return {
            "scenario": "quality_fail",
            "target_component": "COMP-104",
            "detail": "SUP-18 quality_score set to 0.71 (below COMP-104 required threshold 0.85). Hard filter drops SUP-18 before RFQ.",
        }

    elif scenario_name == "insufficient_qty":
        if "SUP-42" in store.suppliers:
            store.suppliers["SUP-42"].available_quantity = 600
        return {
            "scenario": "insufficient_qty",
            "target_component": "COMP-104",
            "detail": "SUP-42 available_quantity capped at 600 (short of COMP-104 950-unit demand). Multi-supplier split required.",
        }

    elif scenario_name == "low_reliability_fastest":
        if "SUP-21" in store.suppliers:
            store.suppliers["SUP-21"].reliability_score = 0.45
        return {
            "scenario": "low_reliability_fastest",
            "target_component": "COMP-104",
            "detail": "SUP-21 reliability score downgraded to 0.45 after contradiction. Solver outranks lead time in favor of reliable split.",
        }

    elif scenario_name == "exceeds_approval":
        from app import config
        config.TRACE_APPROVAL_THRESHOLD = 50000.0
        # Sync every ALREADY-delayed COMP-104 PO's own approval_required_
        # above down to match. This handles disrupt-then-override (e.g.
        # supplier_delay already ran); _mark_po_delayed_and_sync_threshold
        # (above) handles the reverse, override-then-disrupt, at the point a
        # PO transitions to delayed instead — together, order-independent.
        # See that function's docstring for why this is needed at all: once
        # a PO is delayed, RATCHET/SOLVER read ITS OWN threshold, not this
        # global constant.
        for po in store.purchase_orders.values():
            if po.component_id == "COMP-104" and po.status == "delayed":
                po.approval_required_above = 50000.0
        return {
            "scenario": "exceeds_approval",
            "target_component": "COMP-104",
            "detail": "Approval threshold lowered to ₹50,000 (below plan total cost ₹123,674). Ratchet verdict forced to escalate.",
        }

    elif scenario_name == "stale_erp":
        if "COMP-104" in store.inventory:
            store.inventory["COMP-104"].current_stock = 450
            store.inventory["COMP-104"].usable_stock = 390
        _mark_po_delayed_and_sync_threshold(store, "PO-7712")
        return {
            "scenario": "stale_erp",
            "target_component": "COMP-104",
            "detail": "ERP overstates stock (450 current vs 390 warehouse usable). PO-7712 delayed, raising decision-changing inventory contradiction.",
        }

    elif scenario_name == "demand_spike":
        prod = store.production_orders.get("PROD-882")
        if prod is not None:
            prod.units_planned = 1400
        return {
            "scenario": "demand_spike",
            "target_component": "COMP-104",
            "detail": "PROD-882 demand spiked from 700 to 1400 units. Coverage breached, re-entry triggered.",
        }

    elif scenario_name == "expedite_unavailable":
        if "SUP-42" in store.suppliers:
            store.suppliers["SUP-42"].lead_time_days = 8
        return {
            "scenario": "expedite_unavailable",
            "target_component": "COMP-104",
            "detail": "SUP-42 lead_time_days increased from 4 to 8. Pre-committed contingency failure_trigger fires fallback plan.",
        }

    elif scenario_name == "priority_change":
        prod = store.production_orders.get("PROD-914")
        if prod is not None:
            prod.priority = "high"
            prod.deadline = (clock.now() + timedelta(days=3)).date()
        return {
            "scenario": "priority_change",
            "target_component": "COMP-104",
            "detail": "PROD-914 priority elevated to high with 3-day deadline. Production reschedule required.",
        }

    else:
        raise HTTPException(status_code=400, detail=f"Unknown scenario_name: {scenario_name}")

