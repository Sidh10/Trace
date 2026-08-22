"""Data shapes — verbatim from the problem statement §5. Do not rename a
field or invent a data model here (AGENTS.md rule 8, ARCHITECTURE.md §6).

Each record model's docstring cites the problem-statement section its field
set was copied from. Two additions beyond the spec's example JSON are
flagged inline where they occur — both are additive (new fields / new
records of an already-specified shape), never a rename or a reinterpretation
of a given field.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# §5.1 Inventory Database
# --------------------------------------------------------------------------


class InventoryRecord(BaseModel):
    """Verbatim shape, problem statement §5.1."""

    component_id: str
    name: str
    current_stock: int
    usable_stock: int
    daily_usage: int
    safety_stock: int
    warehouse: str
    last_updated: datetime


# --------------------------------------------------------------------------
# §5.2 Purchase Order System
# --------------------------------------------------------------------------

POStatus = Literal["pending", "in_transit", "delayed", "delivered", "cancelled"]


class PurchaseOrder(BaseModel):
    """Verbatim shape, problem statement §5.2."""

    po_id: str
    component_id: str
    supplier_id: str
    quantity: int
    expected_delivery: date
    status: POStatus
    unit_price: float
    total_value: float
    approval_required_above: float


# --------------------------------------------------------------------------
# §5.3 Supplier Catalog
# --------------------------------------------------------------------------


class SupplierRecord(BaseModel):
    """Verbatim shape, problem statement §5.3."""

    supplier_id: str
    supplier_name: str
    component_id: str
    unit_price: float
    lead_time_days: int
    available_quantity: int
    quality_score: float
    reliability_score: float
    min_order_quantity: int
    certifications: list[str]


# --------------------------------------------------------------------------
# §5.4 Production Schedule
# --------------------------------------------------------------------------

Priority = Literal["low", "medium", "high"]


class ProductionOrder(BaseModel):
    """Verbatim shape, problem statement §5.4."""

    production_order_id: str
    product: str
    required_component: str
    units_planned: int
    component_required_per_unit: int
    deadline: date
    priority: Priority


# --------------------------------------------------------------------------
# §5.5 Simulated Email Inbox
#
# The spec's example is unstructured (From / Subject / body text). ADDITION:
# wrapped in a structured envelope (message_id, supplier_id, po_id,
# direction, sent_at) so the environment can store and serve a history —
# every field in the spec's example (from/to/subject/body) is preserved
# unchanged; nothing spec-given is renamed.
# --------------------------------------------------------------------------

MessageDirection = Literal["inbound", "outbound"]


class SupplierMessage(BaseModel):
    message_id: str
    from_: str = Field(alias="from")
    to: str
    subject: str
    body: str
    supplier_id: str
    po_id: Optional[str] = None
    direction: MessageDirection
    sent_at: datetime

    model_config = ConfigDict(populate_by_name=True)


# --------------------------------------------------------------------------
# §5.6 Supplier Communication Tool
# --------------------------------------------------------------------------


class SupplierMessageRequest(BaseModel):
    """Verbatim shape, problem statement §5.6 (request body)."""

    to: str
    subject: str
    body: str


class SupplierMessageResponse(BaseModel):
    message_id: str
    delivered: bool
    sent_at: datetime
    # Populated when the environment's scripted supplier model produced an
    # immediate reply (§4.4). None means no reply yet — poll GET /inbox.
    reply_message_id: Optional[str] = None


# --------------------------------------------------------------------------
# §5.7 RFQ Tool
# --------------------------------------------------------------------------


class RFQRequest(BaseModel):
    component_id: str
    quantity: int
    needed_by: Optional[date] = None
    # None = quote every eligible supplier for this component (spec doesn't
    # constrain the request to one supplier; the response is a list either way).
    supplier_id: Optional[str] = None


class RFQQuote(BaseModel):
    """Verbatim shape, problem statement §5.7."""

    supplier_id: str
    component_id: str
    quantity_available: int
    unit_price: float
    delivery_days: int
    expedite_available: bool
    expedite_fee: float
    quote_valid_hours: int
    # ADDITION: issuance timestamp — required to enforce quote_valid_hours
    # as a real constraint (ARCHITECTURE.md §4, item 4). Not a spec field
    # rename; quote_valid_hours itself is untouched.
    quote_issued_at: datetime


# --------------------------------------------------------------------------
# §5.8 Budget and Approval Tool
# --------------------------------------------------------------------------


class ApprovalCheckRequest(BaseModel):
    action: str
    estimated_cost: float
    # ADDITION: optional — when set, the PO's own approval_required_above
    # (§5.2) governs instead of the global default. Omit to use the global
    # autonomous purchase threshold (§5.8 example: 150000).
    po_id: Optional[str] = None


class ApprovalCheckResponse(BaseModel):
    """Verbatim shape, problem statement §5.8."""

    action: str
    estimated_cost: float
    approval_required: bool
    approval_reason: Optional[str] = None


# --------------------------------------------------------------------------
# §5.9 ERP Update Tool — the one irreversible action (AGENTS.md rule 5)
# --------------------------------------------------------------------------

ERPAction = Literal[
    "mark_po_delayed",
    "create_alternate_po",
    "attach_notes",
    "update_risk_status",
    "record_escalation",
    "store_plan",
]


class ERPUpdateRequest(BaseModel):
    action: ERPAction
    po_id: Optional[str] = None
    production_order_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class ERPUpdateResponse(BaseModel):
    update_id: str
    action: ERPAction
    applied_at: datetime
    resulting_state: dict


# --------------------------------------------------------------------------
# §5.10 Tracking or Verification Tool
# --------------------------------------------------------------------------


class TrackingRecord(BaseModel):
    """Verbatim shape, problem statement §5.10.

    supplier_claim is carried on this model to match the spec's example
    exactly, but GET /tracking/{po_id} has no request body to supply it —
    the environment leaves it null on that read. VERIFY (item 3, not built
    yet) is what actually compares a parsed claim against tracking_status;
    it is not this record's job to store the claim.
    """

    po_id: str
    supplier_claim: Optional[str] = None
    tracking_status: str
    last_movement: Optional[str] = None
