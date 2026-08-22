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
