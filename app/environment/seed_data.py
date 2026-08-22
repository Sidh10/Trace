"""Sample records + generated dataset — ARCHITECTURE.md §6, problem
statement §5 and §16.

The anchor records (COMP-104, Pune-Plant-1, SUP-21/42/37/18, PO-7712,
PROD-882, PROD-914) are copied verbatim where the problem statement gives
JSON for them (§5.1–§5.4, §8, §17). SUP-21, SUP-37, SUP-18 and PROD-914 are
*named* in the spec's narrative (§8 Scenario 3, §17's worked example) but
never given as JSON — those four records are filled in here using the exact
schemas from schemas.py, with values chosen to match what the narrative
requires of them (SUP-18 fails COMP-104's required_quality_score per
Scenario 4's model answer — certified, but quality-rejected, see its own
record below for why; SUP-21 is PO-7712's supplier; PROD-914 is the
low-priority order delayed in §17).

Everything beyond those anchors is generated, deterministically (seeded
RNG), to reach the suggested dataset size in problem statement §16:
20-50 components, 10-20 suppliers, 20-40 POs, 5-10 production orders,
10-20 supplier messages. This is dataset *volume*, not a new data model —
every generated record uses the same schemas.py shapes as the anchors.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional, TypeVar

from pydantic import BaseModel

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
    SupplierMessageResponse,
    SupplierRecord,
    TrackingRecord,
)

_RNG_SEED = 42


# ==========================================================================
# Anchor records — verbatim / narrative-implied, per module docstring above
# ==========================================================================

_ANCHOR_INVENTORY = InventoryRecord(
    component_id="COMP-104",
    name="Motor Driver IC",
    current_stock=420,
    usable_stock=390,
    daily_usage=90,
    safety_stock=150,
    warehouse="Pune-Plant-1",
    last_updated=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
    # ADDITIVE (schemas.py) — the problem statement's own model answer for
    # Scenario 4 rejects SUP-18 on quality_score (0.71), not on
    # certifications. ISO-9001 is required because SUP-21/42/37 already all
    # hold it (a real, pre-existing requirement, not invented); 0.85 sits
    # strictly between SUP-18 (0.71, fails) and SUP-37 (0.89, the next
    # lowest, passes) so the quality check is the sole, unambiguous
    # discriminator — see SUP-18's own record below for why its
    # certifications were also updated to make this true.
    required_certifications=["ISO-9001"],
    required_quality_score=0.85,
)

_ANCHOR_PO = PurchaseOrder(
    po_id="PO-7712",
    component_id="COMP-104",
    supplier_id="SUP-21",
    quantity=1000,
    expected_delivery=date(2026, 9, 4),
    status="in_transit",
    unit_price=118,
    total_value=118000,
    approval_required_above=150000,
)

_ANCHOR_SUPPLIERS = [
    # SUP-21 — PO-7712's own supplier (narrative-implied). §8 Scenario 1/3.
    SupplierRecord(
        supplier_id="SUP-21",
        supplier_name="Ashoka Precision Components",
        component_id="COMP-104",
        unit_price=118,
        lead_time_days=3,
        available_quantity=1000,
        quality_score=0.90,
        reliability_score=0.75,  # starting point; VERIFY (item 3) downgrades on contradiction
        min_order_quantity=500,
        certifications=["ISO-9001"],
    ),
    # SUP-42 — verbatim, §5.3.
    SupplierRecord(
        supplier_id="SUP-42",
        supplier_name="Western Components Ltd",
        component_id="COMP-104",
        unit_price=132,
        lead_time_days=4,
        available_quantity=700,
        quality_score=0.94,
        reliability_score=0.81,
        min_order_quantity=300,
        certifications=["ISO-9001", "Automotive-Grade"],
    ),
    # SUP-37 — narrative-implied, §17: takes the other half of the split
    # (300 units, 6-day delivery), certified.
    SupplierRecord(
        supplier_id="SUP-37",
        supplier_name="Deccan Electro Systems",
        component_id="COMP-104",
        unit_price=126,
        lead_time_days=6,
        available_quantity=450,
        quality_score=0.89,
        reliability_score=0.86,
        min_order_quantity=200,
        certifications=["ISO-9001"],
    ),
    # SUP-18 — narrative-implied, §8 Scenario 4 / §17: cheapest and fastest,
    # rejected on QUALITY SCORE — the problem statement's own model answer,
    # not a certification failure (that earlier reading was wrong; see
    # solver.py's module docstring and OPEN_ITEMS.md). Holds ISO-9001 (the
    # same certification COMP-104 requires and every other COMP-104 supplier
    # already carries) specifically so the certification hard-filter check
    # PASSES for SUP-18 and the quality-score check is demonstrably the sole
    # reason it's dropped — proving the two checks are independent, not that
    # one accidentally masks the other.
    SupplierRecord(
        supplier_id="SUP-18",
        supplier_name="Konkan Fast Components",
        component_id="COMP-104",
        unit_price=108,
        lead_time_days=3,
        available_quantity=900,
        quality_score=0.71,  # below COMP-104's required_quality_score (0.85)
        reliability_score=0.68,
        min_order_quantity=250,
        certifications=["ISO-9001"],
    ),
]

_ANCHOR_PRODUCTION = [
    # PROD-882 — verbatim, §5.4.
    ProductionOrder(
        production_order_id="PROD-882",
        product="Smart Controller Unit",
        required_component="COMP-104",
        units_planned=700,
        component_required_per_unit=1,
        deadline=date(2026, 9, 6),
        priority="high",
    ),
    # PROD-914 — narrative-implied, §17: the low-priority order delayed by
    # 2 days to preserve safety stock. Also draws on COMP-104 — that's what
    # makes it a candidate for delay when PROD-882 needs priority.
    ProductionOrder(
        production_order_id="PROD-914",
        product="Auxiliary Drive Board",
        required_component="COMP-104",
        units_planned=250,
        component_required_per_unit=1,
        deadline=date(2026, 9, 10),
        priority="low",
    ),
]

# §5.5 — verbatim inbound message, wrapped in the structured envelope
# (see schemas.py SupplierMessage docstring for why the envelope is additive).
_ANCHOR_MESSAGE_BODY = (
    "Due to transport issues, delivery may be delayed by 5-7 days. "
    "We are trying to resolve this and will update soon."
)


# Every ID above is either verbatim from the problem statement or pinned by its
# narrative — the demo script (PROJECT.md §4) and §17's worked example both name
# these records directly. A generated record that reuses one of these IDs would
# be silently swallowed by Store's ID-keyed dicts, replacing a spec record with
# a random one and breaking the demo with no error. Every generation range below
# is therefore chosen to sit outside these, and _assert_anchor_ids_reserved()
# enforces that at build time rather than trusting the comments to stay true.
_RESERVED_ANCHOR_IDS: dict[str, frozenset[str]] = {
    "component_id": frozenset({"COMP-104"}),
    "po_id": frozenset({"PO-7712"}),
    "supplier_id": frozenset({"SUP-21", "SUP-42", "SUP-37", "SUP-18"}),
    "production_order_id": frozenset({"PROD-882", "PROD-914"}),
}


# ==========================================================================
# Generated dataset — deterministic, fills out §16's suggested dataset size
# ==========================================================================

# COMP-104 is deliberately absent — it is the anchor component (§5.1), defined
# above. Enforced by _assert_anchor_ids_reserved().
_EXTRA_COMPONENT_DEFS = [
    ("COMP-101", "Power Regulator Module"),
    ("COMP-102", "USB-C Charging Controller"),
    ("COMP-103", "Bluetooth Radio Module"),
    ("COMP-105", "8-Channel Relay Board"),
    ("COMP-106", "Precision Current Sensor"),
    ("COMP-107", "Industrial Temperature Sensor"),
    ("COMP-108", "CAN Bus Transceiver"),
    ("COMP-109", "Flash Memory Module 32GB"),
    ("COMP-110", "LiPo Battery Pack 3S"),
    ("COMP-111", "Stepper Motor Driver"),
    ("COMP-112", "RS-485 Interface IC"),
    ("COMP-113", "Ethernet PHY Chip"),
    ("COMP-114", "Pressure Transducer"),
    ("COMP-115", "LED Driver IC"),
    ("COMP-116", "Optical Isolator"),
    ("COMP-117", "MEMS Accelerometer"),
    ("COMP-118", "Voltage Reference IC"),
    ("COMP-119", "Cooling Fan Assembly"),
    ("COMP-120", "Touchscreen Controller"),
    ("COMP-121", "GPS Receiver Module"),
    ("COMP-122", "Servo Motor Actuator"),
    ("COMP-123", "Capacitive Proximity Sensor"),
]

_SUPPLIER_NAME_POOL = [
    "Bharat Electro Traders",
    "Sahyadri Semiconductors",
    "Pune Precision Parts",
    "Vidarbha Component Works",
    "Sindhudurg Circuits",
    "Nashik Electro Supply",
    "Kolhapur Industrial Components",
    "Aurangabad Micro Devices",
    "Satara Electronics Hub",
    "Solapur Circuit Solutions",
    "Ratnagiri Sensor Systems",
    "Thane Component Exchange",
]

_CERT_POOL = ["ISO-9001", "Automotive-Grade", "RoHS", "IATF-16949"]

_WAREHOUSES = ["Pune-Plant-1", "Pune-Plant-2", "Chakan-Warehouse"]

_PRODUCTS = [
    "Smart Controller Unit",
    "Industrial Gateway Module",
    "EV Battery Management Unit",
    "Auxiliary Drive Board",
    "Robotic Arm Controller",
    "Fleet Telematics Unit",
    "Solar Inverter Control Board",
]


def _make_inventory(rng: random.Random, component_id: str, name: str) -> InventoryRecord:
    daily_usage = rng.choice([20, 30, 40, 55, 60, 75, 90, 110])
    safety_stock_mult = rng.choice([1, 2, 3])
    extra_days = rng.randint(22, 28)
    overstate_gap = rng.randint(0, 40)
    if component_id == "COMP-122":
        daily_usage = 40
        safety_stock = 0
        usable = 420
    else:
        safety_stock = daily_usage * safety_stock_mult
        usable = safety_stock + daily_usage * extra_days
    current = usable + overstate_gap
    return InventoryRecord(
        component_id=component_id,
        name=name,
        current_stock=current,
        usable_stock=usable,
        daily_usage=daily_usage,
        safety_stock=safety_stock,
        warehouse=rng.choice(_WAREHOUSES),
        last_updated=clock.now(),
    )


def _make_suppliers_for(
    rng: random.Random, component_id: str, base_price: float, count: int
) -> list[SupplierRecord]:
    suppliers: list[SupplierRecord] = []
    for _ in range(count):
        # Three digits (SUP-500..SUP-999) — structurally cannot collide with the
        # two-digit anchors SUP-21/42/37/18. _build_extra_components also seeds
        # used_ids with them as a second guard.
        sid = f"SUP-{rng.randint(50, 99)}{rng.randint(0, 9)}"
        price = round(base_price * rng.uniform(0.85, 1.25), 2)
        certs = rng.sample(_CERT_POOL, k=rng.randint(0, 2))
        suppliers.append(
            SupplierRecord(
                supplier_id=sid,
                supplier_name=rng.choice(_SUPPLIER_NAME_POOL),
                component_id=component_id,
                unit_price=price,
                lead_time_days=rng.randint(2, 10),
                available_quantity=rng.randint(150, 1200),
                quality_score=round(rng.uniform(0.65, 0.98), 2),
                reliability_score=round(rng.uniform(0.55, 0.95), 2),
                min_order_quantity=rng.choice([50, 100, 150, 200, 300]),
                certifications=certs,
            )
        )
    return suppliers


def _build_extra_components(rng: random.Random) -> tuple[list[InventoryRecord], list[SupplierRecord]]:
    inventory: list[InventoryRecord] = []
    suppliers: list[SupplierRecord] = []
    used_ids: set[str] = {"SUP-21", "SUP-42", "SUP-37", "SUP-18"}

    # Every extra component gets an inventory row, but only a subset gets
    # catalog suppliers — keeps total suppliers inside §16's suggested
    # 10-20, alongside the 4 anchor suppliers.
    supplied_components = set(
        rng.sample([c for c, _ in _EXTRA_COMPONENT_DEFS], k=10)
    )

    for component_id, name in _EXTRA_COMPONENT_DEFS:
        inv = _make_inventory(rng, component_id, name)
        inventory.append(inv)
        if component_id not in supplied_components:
            continue
        base_price = round(rng.uniform(15, 220), 2)
        for supplier in _make_suppliers_for(rng, component_id, base_price, count=1):
            while supplier.supplier_id in used_ids:
                supplier.supplier_id = f"SUP-{rng.randint(50, 99)}{rng.randint(0, 9)}"
            used_ids.add(supplier.supplier_id)
            suppliers.append(supplier)
    return inventory, suppliers


def _build_purchase_orders(
    rng: random.Random, suppliers: list[SupplierRecord]
) -> list[PurchaseOrder]:
    """`suppliers` must be the GENERATED suppliers only — never the anchors.

    COMP-104's open-PO set is exactly what the problem statement gives it:
    PO-7712, and nothing else. Generating extra POs against the anchor
    suppliers would put thousands of additional COMP-104 units in flight
    beside it, and PROJECT.md §4 Beat 2 depends on PO-7712 being the *only*
    thing standing between PROD-882 and a stock-out — delay it and coverage
    has to fall to the on-hand floor of 390/90 = 4.3 days. With eight extra
    POs alongside it the delay barely moves the number and the demo's central
    beat silently stops working.

    Generated volume exists to satisfy §16's suggested dataset size, and the
    other 22 components carry it. It must not be built on top of the anchor
    scenario.
    """
    # Deep-copied: the module-level anchors are templates, not shared state.
    # See _fresh() below.
    pos = [_fresh(_ANCHOR_PO)]
    statuses = ["pending", "in_transit", "delayed", "delivered"]
    # Two POs per supplier (not just one) to reach §16's suggested 20-40
    # POs against a supplier catalog sized for the 10-20 supplier range.
    rounds = list(suppliers) * 2
    for i, supplier in enumerate(rounds):
        qty = rng.randint(supplier.min_order_quantity, max(supplier.min_order_quantity + 400, supplier.min_order_quantity))
        total = round(qty * supplier.unit_price, 2)
        pos.append(
            PurchaseOrder(
                # 7800+ avoids colliding with the anchor PO-7712.
                po_id=f"PO-{7800 + i}",
                component_id=supplier.component_id,
                supplier_id=supplier.supplier_id,
                quantity=qty,
                expected_delivery=(clock.now() + timedelta(days=rng.randint(2, 14))).date(),
                status=rng.choice(statuses),
                unit_price=supplier.unit_price,
                total_value=total,
                approval_required_above=150000,
            )
        )
    return pos


def _build_production_orders(rng: random.Random, component_ids: list[str]) -> list[ProductionOrder]:
    orders = [_fresh(o) for o in _ANCHOR_PRODUCTION]
    priorities = ["low", "medium", "high"]
    # PROD-900..PROD-904. The anchors are PROD-882 (below the range) and PROD-914
    # (10 above its start) — widening this loop past range(14) would silently
    # overwrite PROD-914, the order §17 reschedules. _assert_anchor_ids_reserved()
    # turns that into a build-time failure instead of a broken demo.
    for i in range(5):
        component_id = rng.choice(component_ids)
        orders.append(
            ProductionOrder(
                production_order_id=f"PROD-{900 + i}",
                product=rng.choice(_PRODUCTS),
                required_component=component_id,
                units_planned=rng.randint(100, 900),
                component_required_per_unit=rng.choice([1, 1, 1, 2]),
                deadline=(clock.now() + timedelta(days=rng.randint(3, 21))).date(),
                priority=rng.choice(priorities),
            )
        )
    return orders


def _tracking_status_for(po: PurchaseOrder) -> tuple[str, Optional[str]]:
    if po.po_id == "PO-7712":
        # §5.10 / §8 Scenario 3 — verbatim: label created, no pickup.
        return "label_created_no_pickup", None
    mapping = {
        "pending": ("not_shipped", None),
        "in_transit": ("in_transit", "departed_origin_facility"),
        "delayed": ("label_created_no_pickup", None),
        "delivered": ("delivered", "arrived_destination"),
        "cancelled": ("not_shipped", None),
    }
    return mapping[po.status]


def _build_messages(rng: random.Random, pos: list[PurchaseOrder]) -> list[SupplierMessage]:
    messages: list[SupplierMessage] = [
        SupplierMessage(
            message_id="MSG-0001",
            **{"from": "supplier21@example.com"},
            to="procurement@trace-demo.local",
            subject="Delay on PO-7712",
            body=_ANCHOR_MESSAGE_BODY,
            supplier_id="SUP-21",
            po_id="PO-7712",
            direction="inbound",
            sent_at=clock.now(),
        )
    ]
    templates = [
        "Confirming your order is on schedule. Expected delivery unchanged.",
        "We are experiencing a short delay of 1-2 days due to raw material sourcing.",
        "Requested quantity is available; please confirm the PO to proceed.",
        "Unable to meet the requested quantity in full — partial shipment possible.",
        "Quality certification documents attached as requested.",
        "Expedited shipping is no longer available for this lane this week.",
    ]
    idx = 2
    # Any open PO can draw supplier correspondence — one of the templates below
    # ("please confirm the PO to proceed") is specifically a pending-PO message.
    # Capped at 13 so the total stays inside §16's suggested 10-20 messages
    # alongside the anchor message above.
    open_pos = [po for po in pos if po.status in ("pending", "delayed", "in_transit")][:13]
    for po in open_pos:
        messages.append(
            SupplierMessage(
                message_id=f"MSG-{idx:04d}",
                **{"from": f"{po.supplier_id.lower()}@example.com"},
                to="procurement@trace-demo.local",
                subject=f"Update on {po.po_id}",
                body=rng.choice(templates),
                supplier_id=po.supplier_id,
                po_id=po.po_id,
                direction="inbound",
                sent_at=clock.now(),
            )
        )
        idx += 1
    return messages


class Store:
    """In-memory, mutable environment state. One process-wide instance
    (`STATE`, below). Every mutation happens through a method here so
    routes.py stays a thin HTTP layer over this object."""

    def __init__(
        self,
        inventory: list[InventoryRecord],
        purchase_orders: list[PurchaseOrder],
        suppliers: list[SupplierRecord],
        production_orders: list[ProductionOrder],
        messages: list[SupplierMessage],
    ) -> None:
        self.inventory: dict[str, InventoryRecord] = {r.component_id: r for r in inventory}
        self.purchase_orders: dict[str, PurchaseOrder] = {r.po_id: r for r in purchase_orders}
        self.suppliers: dict[str, SupplierRecord] = {r.supplier_id: r for r in suppliers}
        self.production_orders: dict[str, ProductionOrder] = {
            r.production_order_id: r for r in production_orders
        }
        self.messages: dict[str, SupplierMessage] = {m.message_id: m for m in messages}
        self.tracking: dict[str, TrackingRecord] = {}
        for po in self.purchase_orders.values():
            status, movement = _tracking_status_for(po)
            self.tracking[po.po_id] = TrackingRecord(
                po_id=po.po_id,
                supplier_claim=None,
                tracking_status=status,
                last_movement=movement,
            )
        self.rfq_log: list[RFQQuote] = []
        self.erp_log: list[ERPUpdateResponse] = []
        self._message_seq = len(self.messages) + 1
        self._erp_seq = 1
        self._rng = random.Random(_RNG_SEED + 1)

    # -- reads ------------------------------------------------------------

    def list_inventory(self) -> list[InventoryRecord]:
        return list(self.inventory.values())

    def get_inventory(self, component_id: str) -> Optional[InventoryRecord]:
        return self.inventory.get(component_id)

    def list_purchase_orders(
        self, status: Optional[str] = None, component_id: Optional[str] = None
    ) -> list[PurchaseOrder]:
        pos = list(self.purchase_orders.values())
        if status:
            pos = [p for p in pos if p.status == status]
        if component_id:
            pos = [p for p in pos if p.component_id == component_id]
        return pos

    def get_purchase_order(self, po_id: str) -> Optional[PurchaseOrder]:
        return self.purchase_orders.get(po_id)

    def list_suppliers(self, component_id: Optional[str] = None) -> list[SupplierRecord]:
        suppliers = list(self.suppliers.values())
        if component_id:
            suppliers = [s for s in suppliers if s.component_id == component_id]
        return suppliers

    def get_supplier(self, supplier_id: str) -> Optional[SupplierRecord]:
        return self.suppliers.get(supplier_id)

    def list_production_schedule(self) -> list[ProductionOrder]:
        return list(self.production_orders.values())

    def list_inbox(
        self, supplier_id: Optional[str] = None, po_id: Optional[str] = None
    ) -> list[SupplierMessage]:
        msgs = list(self.messages.values())
        if supplier_id:
            msgs = [m for m in msgs if m.supplier_id == supplier_id]
        if po_id:
            msgs = [m for m in msgs if m.po_id == po_id]
        return sorted(msgs, key=lambda m: m.sent_at)

    def get_message(self, message_id: str) -> Optional[SupplierMessage]:
        return self.messages.get(message_id)

    def get_tracking(self, po_id: str) -> Optional[TrackingRecord]:
        return self.tracking.get(po_id)

    # -- writes -------------------------------------------------------------

    def _next_message_id(self) -> str:
        mid = f"MSG-{self._message_seq:04d}"
        self._message_seq += 1
        return mid

    def send_supplier_message(
        self, supplier_id: str, to: str, subject: str, body: str
    ) -> SupplierMessageResponse:
        """§5.6. Records the outbound message, then the environment's
        scripted supplier model (deterministic — no LLM in the simulator,
        AGENTS.md rule 1) may append an immediate inbound reply."""
        outbound = SupplierMessage(
            message_id=self._next_message_id(),
            **{"from": "procurement@trace-demo.local"},
            to=to,
            subject=subject,
            body=body,
            supplier_id=supplier_id,
            po_id=self._infer_po_id(subject, body),
            direction="outbound",
            sent_at=clock.now(),
        )
        self.messages[outbound.message_id] = outbound

        reply = self._scripted_reply(supplier_id, outbound)
        reply_id = None
        if reply is not None:
            self.messages[reply.message_id] = reply
            reply_id = reply.message_id

        return SupplierMessageResponse(
            message_id=outbound.message_id,
            delivered=True,
            sent_at=outbound.sent_at,
            reply_message_id=reply_id,
        )

    def _infer_po_id(self, subject: str, body: str) -> Optional[str]:
        for po_id in self.purchase_orders:
            if po_id in subject or po_id in body:
                return po_id
        return None

    def _scripted_reply(
        self, supplier_id: str, outbound: SupplierMessage
    ) -> Optional[SupplierMessage]:
        supplier = self.suppliers.get(supplier_id)
        if supplier is None:
            return None

        # §8 Scenario 3, verbatim narrative: SUP-21 claims dispatched;
        # tracking (already seeded above) contradicts it.
        if supplier_id == "SUP-21" and outbound.po_id == "PO-7712":
            body = (
                "Thanks for checking in — PO-7712 has been dispatched as of "
                "this morning. Tracking should update shortly."
            )
        elif outbound.po_id and outbound.po_id in self.purchase_orders:
            po = self.purchase_orders[outbound.po_id]
            body = (
                f"Confirming PO {po.po_id}: {po.quantity} units of "
                f"{po.component_id}, currently {po.status}, expected "
                f"{po.expected_delivery.isoformat()}."
            )
        else:
            body = (
                f"We can supply {supplier.available_quantity} units of "
                f"{supplier.component_id} at {supplier.unit_price}/unit, "
                f"{supplier.lead_time_days}-day lead time, MOQ "
                f"{supplier.min_order_quantity}."
            )

        return SupplierMessage(
            message_id=self._next_message_id(),
            **{"from": f"{supplier_id.lower()}@example.com"},
            to=outbound.from_,
            subject=f"RE: {outbound.subject}",
            body=body,
            supplier_id=supplier_id,
            po_id=outbound.po_id,
            direction="inbound",
            sent_at=clock.now(),
        )

    def request_rfq(self, req: RFQRequest) -> list[RFQQuote]:
        """§5.7. Hard hasattr filter for eligibility (component match,
        supplier filter if given) happens here; certification / budget
        filtering is SOLVER's job (item 4), not the environment's."""
        candidates = self.list_suppliers(component_id=req.component_id)
        if req.supplier_id:
            candidates = [s for s in candidates if s.supplier_id == req.supplier_id]

        quotes: list[RFQQuote] = []
        for supplier in candidates:
            available = min(supplier.available_quantity, req.quantity) if req.quantity else supplier.available_quantity
            # small deterministic jitter so a fresh RFQ isn't byte-identical
            # to the catalog price every time — real RFQ desks quote fresh.
            price = round(supplier.unit_price * self._rng.uniform(0.98, 1.05), 2)
            expedite_available = supplier.lead_time_days > 2
            quotes.append(
                RFQQuote(
                    supplier_id=supplier.supplier_id,
                    component_id=supplier.component_id,
                    quantity_available=available,
                    unit_price=price,
                    delivery_days=supplier.lead_time_days,
                    expedite_available=expedite_available,
                    expedite_fee=round(price * available * 0.08, 2) if expedite_available else 0.0,
                    quote_valid_hours=6,  # §5.7 verbatim — quote_valid_hours: 6
                    quote_issued_at=clock.now(),
                )
            )
        self.rfq_log.extend(quotes)
        return quotes

    def check_approval(self, req: ApprovalCheckRequest) -> ApprovalCheckResponse:
        """§5.8. Threshold comes from the named PO when given, else the
        global autonomous purchase threshold (config.TRACE_APPROVAL_THRESHOLD,
        default 150000 — matches the spec's own example)."""
        from app.config import TRACE_APPROVAL_THRESHOLD  # local import avoids a cycle

        threshold = TRACE_APPROVAL_THRESHOLD
        if req.po_id and req.po_id in self.purchase_orders:
            threshold = self.purchase_orders[req.po_id].approval_required_above

        approval_required = req.estimated_cost > threshold
        reason = None
        if approval_required:
            reason = (
                f"Cost exceeds autonomous purchase threshold of {threshold:.0f}"
            )
        return ApprovalCheckResponse(
            action=req.action,
            estimated_cost=req.estimated_cost,
            approval_required=approval_required,
            approval_reason=reason,
        )

    def update_erp(self, req: ERPUpdateRequest) -> ERPUpdateResponse:
        """§5.9. The one irreversible action (AGENTS.md rule 5) — every
        other method on this class is an idempotent read or a compensable
        message. Applies the action to the relevant record(s) and returns
        the resulting state for the audit trail (item 7, not built yet)."""
        resulting_state: dict = {}

        if req.action == "mark_po_delayed":
            po = self._require_po(req.po_id)
            po.status = "delayed"
            resulting_state = {"po_id": po.po_id, "status": po.status}

        elif req.action == "create_alternate_po":
            new_id = req.payload.get("po_id") or f"PO-{9000 + len(self.purchase_orders)}"
            new_po = PurchaseOrder(
                po_id=new_id,
                component_id=req.payload["component_id"],
                supplier_id=req.payload["supplier_id"],
                quantity=req.payload["quantity"],
                expected_delivery=req.payload["expected_delivery"],
                status="pending",
                unit_price=req.payload["unit_price"],
                total_value=req.payload["quantity"] * req.payload["unit_price"],
                approval_required_above=req.payload.get("approval_required_above", 150000),
            )
            self.purchase_orders[new_id] = new_po
            status, movement = _tracking_status_for(new_po)
            self.tracking[new_id] = TrackingRecord(
                po_id=new_id, supplier_claim=None, tracking_status=status, last_movement=movement
            )
            resulting_state = new_po.model_dump(mode="json")

        elif req.action == "attach_notes":
            po = self._require_po(req.po_id)
            resulting_state = {"po_id": po.po_id, "note": req.payload.get("note", "")}

        elif req.action == "update_risk_status":
            prod = self._require_production_order(req.production_order_id)
            resulting_state = {
                "production_order_id": prod.production_order_id,
                "risk_status": req.payload.get("risk_status", "unspecified"),
            }

        elif req.action == "record_escalation":
            resulting_state = dict(req.payload)

        elif req.action == "store_plan":
            resulting_state = dict(req.payload)

        update = ERPUpdateResponse(
            update_id=f"ERP-{self._erp_seq:04d}",
            action=req.action,
            applied_at=clock.now(),
            resulting_state=resulting_state,
        )
        self._erp_seq += 1
        self.erp_log.append(update)
        return update

    def _require_po(self, po_id: Optional[str]) -> PurchaseOrder:
        if not po_id or po_id not in self.purchase_orders:
            raise KeyError(f"unknown po_id: {po_id!r}")
        return self.purchase_orders[po_id]

    def _require_production_order(self, production_order_id: Optional[str]) -> ProductionOrder:
        if not production_order_id or production_order_id not in self.production_orders:
            raise KeyError(f"unknown production_order_id: {production_order_id!r}")
        return self.production_orders[production_order_id]


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _fresh(record: _ModelT) -> _ModelT:
    """Deep-copy an anchor record.

    The _ANCHOR_* constants are templates. Handing the same object to every
    Store makes them shared mutable state: marking PO-7712 delayed in one run
    would silently leave it delayed in the next `build_store()`, so the
    environment could never actually be reset. That breaks the judge-controlled
    disruption panel (ARCHITECTURE.md item 11), where PROJECT.md §4 Beat 5
    hands over the controls and the environment has to return to a known state
    between injections — and it breaks quietly, as a demo that behaves
    differently the second time a judge presses the button.
    """
    return record.model_copy(deep=True)


def _assert_anchor_ids_reserved(
    inventory: list[InventoryRecord],
    purchase_orders: list[PurchaseOrder],
    suppliers: list[SupplierRecord],
    production_orders: list[ProductionOrder],
) -> None:
    """Fail the build if generation collided with an anchor ID.

    Two distinct failures are checked, because Store keys every collection by
    ID and a dict would hide both:

    1. Any duplicate ID at all — the second record wins and the first vanishes
       with no error, and the collection count still looks correct.
    2. An anchor ID missing entirely — it was never emitted, or a generated
       record displaced it upstream of this call.

    Raises AssertionError explicitly rather than using `assert`, so the check
    survives `python -O`.
    """
    collections: list[tuple[str, list[str]]] = [
        ("component_id", [r.component_id for r in inventory]),
        ("po_id", [r.po_id for r in purchase_orders]),
        ("supplier_id", [r.supplier_id for r in suppliers]),
        ("production_order_id", [r.production_order_id for r in production_orders]),
    ]

    for field, ids in collections:
        counts: dict[str, int] = {}
        for record_id in ids:
            counts[record_id] = counts.get(record_id, 0) + 1

        duplicates = sorted(rid for rid, n in counts.items() if n > 1)
        if duplicates:
            raise AssertionError(
                f"seed_data: duplicate {field} in generated dataset: {duplicates}. "
                "Store keys by ID, so the duplicate would silently replace the "
                "record it collided with."
            )

        for anchor_id in sorted(_RESERVED_ANCHOR_IDS[field]):
            if counts.get(anchor_id, 0) != 1:
                raise AssertionError(
                    f"seed_data: anchor {field} {anchor_id!r} appears "
                    f"{counts.get(anchor_id, 0)} times, expected exactly 1. "
                    "The anchor records are the problem statement's own sample "
                    "data and PROJECT.md §4's demo depends on them verbatim."
                )


def build_store() -> Store:
    rng = random.Random(_RNG_SEED)
    extra_inventory, extra_suppliers = _build_extra_components(rng)

    inventory = [_fresh(_ANCHOR_INVENTORY), *extra_inventory]
    suppliers = [*(_fresh(s) for s in _ANCHOR_SUPPLIERS), *extra_suppliers]
    component_ids = [inv.component_id for inv in inventory]

    # Generated suppliers only — see _build_purchase_orders' docstring. The
    # anchor suppliers' only PO is the spec's own PO-7712.
    purchase_orders = _build_purchase_orders(rng, extra_suppliers)
    production_orders = _build_production_orders(rng, component_ids)
    messages = _build_messages(rng, purchase_orders)

    _assert_anchor_ids_reserved(inventory, purchase_orders, suppliers, production_orders)

    return Store(
        inventory=inventory,
        purchase_orders=purchase_orders,
        suppliers=suppliers,
        production_orders=production_orders,
        messages=messages,
    )


# Single process-wide store, built at import time. routes.py imports STATE,
# not build_store — one dataset per process, mutated in place as the demo runs.
STATE = build_store()
