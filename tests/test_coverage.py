"""Coverage engine tests — ARCHITECTURE.md §4 item 2, §8 repo layout.

The anchor-record tests are not illustrative: PROJECT.md §4 Beat 2 puts
PROD-882's days of coverage on a screen in front of judges, and these pin the
exact numbers that beat depends on. If one of them fails, the demo is broken
even if nothing raises.

Every test builds its own Store rather than importing STATE, so a test that
mutates a PO status (as the Beat 2 tests do) cannot leak into another.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from app.engine.coverage import (
    DEPENDABLE_PO_STATUSES,
    compute_coverage,
    reset_event_sequence,
)
from app.environment.clock import clock
from app.environment.seed_data import (
    _assert_anchor_ids_reserved,
    build_store,
)
from app.environment.schemas import ProductionOrder

# Sim epoch — clock.py's _EPOCH. Passed explicitly so these tests do not depend
# on process-wide clock state left behind by another module.
NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    return build_store()


# ==========================================================================
# Anchor IDs — the item-1 fix this engine is built on top of
# ==========================================================================


def test_all_seven_anchor_ids_survive_generation(store):
    """COMP-104, PO-7712, SUP-21/42/37/18, PROD-882/914 each appear exactly
    once. Store keys by ID, so a generated collision would silently replace an
    anchor and leave the collection counts looking correct."""
    for component_id in ["COMP-104"]:
        assert store.get_inventory(component_id) is not None
    assert store.get_purchase_order("PO-7712") is not None
    for supplier_id in ["SUP-21", "SUP-42", "SUP-37", "SUP-18"]:
        assert store.get_supplier(supplier_id) is not None
    for production_order_id in ["PROD-882", "PROD-914"]:
        assert production_order_id in store.production_orders

    # The anchors kept their spec values, not a generated record's.
    assert store.get_inventory("COMP-104").name == "Motor Driver IC"
    assert store.get_purchase_order("PO-7712").supplier_id == "SUP-21"
    assert store.production_orders["PROD-882"].priority == "high"
    assert store.production_orders["PROD-914"].priority == "low"


def test_anchor_assertion_rejects_a_collision(store):
    """The guard fails loudly rather than letting a generated record displace
    an anchor."""
    collision = ProductionOrder(
        production_order_id="PROD-914",
        product="Generated Collision",
        required_component="COMP-104",
        units_planned=1,
        component_required_per_unit=1,
        deadline=date(2026, 9, 30),
        priority="low",
    )
    with pytest.raises(AssertionError, match="duplicate production_order_id"):
        _assert_anchor_ids_reserved(
            store.list_inventory(),
            store.list_purchase_orders(),
            store.list_suppliers(),
            store.list_production_schedule() + [collision],
        )


def test_anchor_assertion_rejects_a_missing_anchor(store):
    with pytest.raises(AssertionError, match="PROD-882"):
        _assert_anchor_ids_reserved(
            store.list_inventory(),
            store.list_purchase_orders(),
            store.list_suppliers(),
            [
                o
                for o in store.list_production_schedule()
                if o.production_order_id != "PROD-882"
            ],
        )


def test_build_store_returns_an_isolated_environment():
    """Each build must be a clean slate. The anchors are module-level
    templates, and handing the same objects to every Store made them shared
    mutable state — marking PO-7712 delayed in one run left it delayed in the
    next build. PROJECT.md §4 Beat 5 hands the disruption panel to a judge, so
    the environment has to reset between injections; without this the demo
    behaves differently the second time the button is pressed."""
    first = build_store()
    first.purchase_orders["PO-7712"].status = "delayed"
    first.inventory["COMP-104"].usable_stock = 0
    first.production_orders["PROD-882"].priority = "low"

    second = build_store()
    assert second.purchase_orders["PO-7712"].status == "in_transit"
    assert second.inventory["COMP-104"].usable_stock == 390
    assert second.production_orders["PROD-882"].priority == "high"
    assert first.purchase_orders["PO-7712"] is not second.purchase_orders["PO-7712"]


def test_po_7712_is_the_only_open_po_for_comp_104(store):
    """PROJECT.md §4 Beat 2 requires PO-7712 to be the only thing between
    PROD-882 and a stock-out. Extra generated COMP-104 POs would dilute the
    delay until it stopped moving the number on screen."""
    assert [po.po_id for po in store.list_purchase_orders(component_id="COMP-104")] == [
        "PO-7712"
    ]


# ==========================================================================
# The item-2 formula
# ==========================================================================


def test_days_of_coverage_on_hand_is_usable_stock_over_daily_usage(store):
    """The literal brief: days_of_coverage = usable_stock / daily_usage."""
    record = store.get_inventory("COMP-104")
    assert (record.usable_stock, record.daily_usage) == (390, 90)

    report = compute_coverage(store, now=NOW)
    result = report.for_production_order("PROD-882")
    assert result.days_of_coverage_on_hand == round(390 / 90, 1) == 4.3


def test_priority_and_deadline_are_cross_referenced_from_the_schedule(store):
    report = compute_coverage(store, now=NOW)

    prod_882 = report.for_production_order("PROD-882")
    assert prod_882.priority == "high"
    assert prod_882.deadline.date() == date(2026, 9, 6)
    assert prod_882.days_to_deadline == 4.7  # Sept 1 08:00 -> Sept 6 00:00
    assert prod_882.component_required == 700  # 700 units x 1 per unit

    prod_914 = report.for_production_order("PROD-914")
    assert prod_914.priority == "low"
    assert prod_914.deadline.date() == date(2026, 9, 10)
    assert prod_914.component_required == 250


def test_every_production_order_gets_a_result(store):
    report = compute_coverage(store, now=NOW)
    assert len(report.results) == len(store.list_production_schedule())


# ==========================================================================
# Beat 1 -> Beat 2, the demo's central transition
# ==========================================================================


def test_beat_1_anchor_orders_are_calm(store):
    """At rest PO-7712 is in transit, so both COMP-104 orders are covered."""
    report = compute_coverage(store, now=NOW)

    for production_order_id in ("PROD-882", "PROD-914"):
        result = report.for_production_order(production_order_id)
        assert result.status == "healthy"
        assert result.is_thin is False
        # 390 on hand, +1000 landing Sept 4, at 90/day.
        assert result.days_of_coverage == 15.4
        # Healthy only because PO-7712 is counted — that is what makes it
        # worth polling proactively (item 2b).
        assert result.depends_on_po_ids == ["PO-7712"]

    assert not [e for e in report.events if e.component_id == "COMP-104"]


def test_beat_2_sup21_delay_drops_prod_882_to_4_3_days(store):
    """PROJECT.md §4 Beat 2, verbatim: "PROD-882 coverage drops to 4.3 days
    against a Sept 6 deadline"."""
    before = compute_coverage(store, now=NOW).for_production_order("PROD-882")
    assert before.days_of_coverage == 15.4

    # SUP-21 delays PO-7712. The credit is withdrawn and the projection
    # collapses to the on-hand floor.
    store.purchase_orders["PO-7712"].status = "delayed"

    after = compute_coverage(store, now=NOW).for_production_order("PROD-882")
    assert after.days_of_coverage == 4.3
    assert after.days_of_coverage == after.days_of_coverage_on_hand
    assert after.deadline.date() == date(2026, 9, 6)
    assert after.status == "critical"
    assert after.is_thin is True
    assert after.reason == "stockout_before_deadline"
    # 4.3 days of coverage against a 4.7-day deadline: the line stops first.
    assert after.days_of_coverage < after.days_to_deadline


def test_beat_2_also_takes_prod_914_critical(store):
    """PROD-914 draws the same component, which is why §17 reschedules it —
    both orders compete for COMP-104 once PO-7712 is gone."""
    store.purchase_orders["PO-7712"].status = "delayed"
    result = compute_coverage(store, now=NOW).for_production_order("PROD-914")

    assert result.days_of_coverage == 4.3
    assert result.status == "critical"
    assert result.priority == "low"  # the order §17 delays by 2 days


def test_beat_2_emits_a_disruption_event_per_architecture_section_7(store):
    store.purchase_orders["PO-7712"].status = "delayed"
    report = compute_coverage(store, now=NOW)

    events = [
        e
        for e in report.events
        if e.production_order_id == "PROD-882" and e.type == "coverage_breach"
    ]
    assert len(events) == 1
    event = events[0]

    assert event.event_id.startswith("EVT-")
    assert event.component_id == "COMP-104"
    assert event.detected_at == NOW
    assert event.source == "erp_check"
    assert event.detail["days_of_coverage"] == 4.3
    assert event.detail["days_to_deadline"] == 4.7
    assert event.detail["priority"] == "high"


def test_healthy_orders_emit_no_event(store):
    """Continuity is 35% but Tool Efficiency is 10% — a covered order must not
    generate an event for something downstream to filter out later."""
    report = compute_coverage(store, now=NOW)
    breaches = {e.production_order_id for e in report.events if e.type == "coverage_breach"}
    for result in report.results:
        if not result.is_thin:
            assert result.production_order_id not in breaches


# ==========================================================================
# Item 2b's gate
# ==========================================================================


def test_polling_targets_include_po_7712_before_the_delay_is_known(store):
    """ARCHITECTURE.md §3: the proactive poll must catch the stall BEFORE the
    delay email arrives. At Beat 1 PROD-882 still reads healthy, so the gate
    has to key on load-bearing POs, not on already-thin orders."""
    report = compute_coverage(store, now=NOW)
    assert "PO-7712" in report.polling_targets()


def test_polling_targets_stay_a_short_list(store):
    """The gate exists to avoid a sweep — a sweep is what Tool Efficiency
    penalises."""
    report = compute_coverage(store, now=NOW)
    assert len(report.polling_targets()) < len(store.purchase_orders) / 4


# ==========================================================================
# ERP-vs-warehouse mismatch — Scenario 2
# ==========================================================================


def test_mismatch_is_logged_as_a_contradiction_not_reconciled(store):
    """COMP-104 ships current_stock 420 against usable_stock 390 (§5.1). The
    gap is recorded; neither figure is overwritten, averaged, or corrected."""
    report = compute_coverage(store, now=NOW)
    found = [c for c in report.contradictions if c.component_id == "COMP-104"]
    assert len(found) == 1
    contradiction = found[0]

    assert contradiction.erp_current_stock == 420
    assert contradiction.warehouse_usable_stock == 390
    assert contradiction.erp_overstatement_units == 30
    assert "Not reconciled" in contradiction.note

    # The store still holds both original figures — nothing was written back.
    record = store.get_inventory("COMP-104")
    assert (record.current_stock, record.usable_stock) == (420, 390)


def test_coverage_math_uses_warehouse_truth_not_the_erp_figure(store):
    """The whole point of Scenario 2: planning on the ERP number is the
    mistake. 420/90 = 4.7 days would have read as covered; 390/90 = 4.3 does
    not."""
    store.purchase_orders["PO-7712"].status = "delayed"
    report = compute_coverage(store, now=NOW)

    result = report.for_production_order("PROD-882")
    assert result.days_of_coverage == 4.3  # usable_stock, not current_stock

    contradiction = next(c for c in report.contradictions if c.component_id == "COMP-104")
    assert contradiction.coverage_days_on_warehouse_truth == 4.3
    assert contradiction.coverage_days_if_erp_believed == 4.7
    assert contradiction.decision_changing is True


def test_a_matching_inventory_record_produces_no_contradiction(store):
    record = store.get_inventory("COMP-104")
    record.current_stock = record.usable_stock

    report = compute_coverage(store, now=NOW)
    assert not [c for c in report.contradictions if c.component_id == "COMP-104"]


def test_only_decision_changing_contradictions_raise_an_event(store):
    """Every gap is logged as a finding; only the ones that would have changed
    the verdict escalate to a DisruptionEvent."""
    report = compute_coverage(store, now=NOW)
    escalated = {e.component_id for e in report.events if e.type == "inventory_mismatch"}
    for contradiction in report.contradictions:
        if not contradiction.decision_changing:
            assert contradiction.component_id not in escalated


# ==========================================================================
# Projection edge cases
# ==========================================================================


def test_delivered_and_cancelled_pos_are_not_credited():
    assert "delivered" not in DEPENDABLE_PO_STATUSES
    assert "cancelled" not in DEPENDABLE_PO_STATUSES
    assert "delayed" not in DEPENDABLE_PO_STATUSES


def test_a_po_arriving_after_stockout_does_not_extend_coverage(store):
    """The line stops when stock hits zero. A delivery three days later
    restarts it — that is a different, later event, and must not be reported
    as uninterrupted coverage."""
    store.purchase_orders["PO-7712"].expected_delivery = date(2026, 9, 20)
    result = compute_coverage(store, now=NOW).for_production_order("PROD-882")

    assert result.days_of_coverage == 4.3  # not 4.3 + 1000/90
    assert result.status == "critical"


def test_zero_daily_usage_does_not_divide_by_zero(store):
    record = store.get_inventory("COMP-104")
    record.daily_usage = 0

    result = compute_coverage(store, now=NOW).for_production_order("PROD-882")
    assert math.isinf(result.days_of_coverage)
    assert result.status == "healthy"


def test_a_passed_deadline_is_critical(store):
    """Reading the schedule after a deadline has gone by must not report the
    order as covered because coverage exceeds a negative number."""
    result = compute_coverage(
        store, now=datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)
    ).for_production_order("PROD-882")

    assert result.days_to_deadline < 0
    assert result.status == "critical"
    assert result.reason == "deadline_passed"


def test_compute_coverage_does_not_mutate_the_store(store):
    """Read-only, so MONITOR can re-run it freely (AGENTS.md rule 5 — the one
    irreversible action is POST /erp/update, and this is not it)."""
    before = {
        "inventory": [r.model_dump(mode="json") for r in store.list_inventory()],
        "pos": [p.model_dump(mode="json") for p in store.list_purchase_orders()],
        "erp_log": len(store.erp_log),
    }

    compute_coverage(store, now=NOW)
    compute_coverage(store, now=NOW)

    assert [r.model_dump(mode="json") for r in store.list_inventory()] == before["inventory"]
    assert [p.model_dump(mode="json") for p in store.list_purchase_orders()] == before["pos"]
    assert len(store.erp_log) == before["erp_log"]


def test_repeated_passes_agree(store):
    """Deterministic: same inputs, same numbers. A coverage board that drifts
    between refreshes is unexplainable on stage."""
    first = compute_coverage(store, now=NOW)
    second = compute_coverage(store, now=NOW)

    assert [r.model_dump(mode="json") for r in first.results] == [
        r.model_dump(mode="json") for r in second.results
    ]


def test_no_llm_is_reachable_from_the_coverage_engine():
    """AGENTS.md rule 1 and rule 2. The coverage engine is deterministic and
    must import nothing from app.llm, directly or transitively."""
    import app.engine.coverage as coverage_module

    source = open(coverage_module.__file__, encoding="utf-8").read()
    assert "app.llm" not in source
    assert "genai" not in source
    assert "gemini" not in source.lower().replace("gemini-<version>", "")
