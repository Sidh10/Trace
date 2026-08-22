"""MONITOR tests — ARCHITECTURE.md §4 item 2b.

The load-bearing gate is the whole component, and `test_beat_1_*` below is the
test that decides whether item 2b works at all: at Beat 1 PROD-882 reads
healthy, and PO-7712 must still be polled. An implementation that fires only on
already-thin coverage passes every other test in this file and fails the one
scenario item 2b exists for.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.monitor import (
    IN_FLIGHT_PO_STATUSES,
    NOT_MOVED_TRACKING_STATUSES,
    run_monitor_cycle,
)
from app.environment.clock import clock
from app.environment.seed_data import build_store

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    return build_store()


# ==========================================================================
# The gate — load-bearing, not thin
# ==========================================================================


def test_beat_1_polls_po_7712_while_prod_882_still_reads_healthy(store):
    """The test item 2b exists for.

    PROD-882 is healthy at 15.4 days of coverage — it is *only* healthy because
    PO-7712 is counted. A thin-coverage gate would skip PO-7712 here and catch
    the stall only after the delay email arrived, which is the exact failure
    ARCHITECTURE.md §3 says the proactive poll prevents.
    """
    coverage = compute_coverage(store, now=NOW)
    prod_882 = coverage.for_production_order("PROD-882")

    # Precondition: the order is NOT thin. If this ever flips, the test below
    # stops proving anything.
    assert prod_882.status == "healthy"
    assert prod_882.is_thin is False
    assert prod_882.days_of_coverage == 15.4

    report = run_monitor_cycle(store, coverage=coverage, now=NOW)
    decision = report.for_po("PO-7712")

    assert decision is not None
    assert decision.polled is True
    assert decision.reason == "load_bearing"
    assert "PROD-882" in decision.load_bearing_for


def test_beat_1_poll_catches_the_stall_before_any_supplier_message(store):
    """PO-7712 is recorded in_transit while tracking reads
    label_created_no_pickup (§5.10). The contradiction is between two records —
    no supplier message is read, so nothing here depends on tone
    (AGENTS.md rule 4)."""
    report = run_monitor_cycle(store, now=NOW)
    decision = report.for_po("PO-7712")

    assert decision.po_status == "in_transit"
    assert decision.tracking_status == "label_created_no_pickup"
    assert decision.contradicts_po_status is True

    events = [e for e in report.events if e.po_id == "PO-7712"]
    assert len(events) == 1
    event = events[0]
    assert event.type == "supplier_delay"
    assert event.source == "proactive_poll"
    assert event.component_id == "COMP-104"
    assert event.production_order_id == "PROD-882"  # tightest deadline
    assert event.detected_at == NOW


def test_does_not_poll_pos_that_are_not_load_bearing(store):
    """Every skipped PO is recorded with a reason, and none of them cost a
    tracking call."""
    coverage = compute_coverage(store, now=NOW)
    targets = set(coverage.polling_targets())
    report = run_monitor_cycle(store, coverage=coverage, now=NOW)

    assert targets  # guard: an empty gate would make this vacuous

    for decision in report.decisions:
        if decision.po_id in targets:
            assert decision.polled is True
        else:
            assert decision.polled is False
            assert decision.reason == "not_load_bearing"
            # We did not look, so we report nothing about what tracking said.
            assert decision.tracking_status is None
            assert decision.contradicts_po_status is False


def test_a_non_load_bearing_po_is_not_polled_even_when_it_would_have_fired(store):
    """The sharpest form of the gate: a PO that WOULD produce a finding is
    still skipped, because nothing's coverage depends on it. Being a real
    finding is not the bar — being worth a call is."""
    coverage = compute_coverage(store, now=NOW)
    targets = set(coverage.polling_targets())

    victim = next(
        po
        for po in store.list_purchase_orders()
        if po.po_id not in targets and po.component_id != "COMP-104"
    )
    # Rig it into exactly the state PO-7712 is in.
    victim.status = "in_transit"
    store.tracking[victim.po_id].tracking_status = "label_created_no_pickup"

    coverage = compute_coverage(store, now=NOW)
    assert victim.po_id not in coverage.polling_targets()

    report = run_monitor_cycle(store, coverage=coverage, now=NOW)
    decision = report.for_po(victim.po_id)

    assert decision.polled is False
    assert decision.reason == "not_load_bearing"
    assert not [e for e in report.events if e.po_id == victim.po_id]


def test_poll_count_stays_far_below_the_available_calls(store):
    """Tool Efficiency (10%) is scored on deliberate calls. The gate must be a
    gate, not a sweep."""
    report = run_monitor_cycle(store, now=NOW)

    assert report.polls_made == len(report.polled())
    assert report.polls_available > report.polls_made
    assert report.polls_made <= 3
    assert report.polls_made / report.polls_available < 0.25


def test_closed_pos_are_excluded_from_the_denominator(store):
    """Delivered POs cannot be polled, so counting them would inflate the
    efficiency ratio for free."""
    report = run_monitor_cycle(store, now=NOW)
    counted = {d.po_id for d in report.decisions}

    for po in store.list_purchase_orders():
        if po.status in ("delivered", "cancelled"):
            assert po.po_id not in counted


# ==========================================================================
# Consuming item 2 rather than reimplementing it
# ==========================================================================


def test_exposure_is_the_gap_between_the_two_coverage_metrics(store):
    """ARCHITECTURE.md §3: the gap between days_of_coverage and
    days_of_coverage_on_hand is the exposure to supplier claims. MONITOR
    reports it, it does not compute coverage."""
    coverage = compute_coverage(store, now=NOW)
    prod_882 = coverage.for_production_order("PROD-882")
    expected = round(prod_882.days_of_coverage - prod_882.days_of_coverage_on_hand, 1)

    report = run_monitor_cycle(store, coverage=coverage, now=NOW)
    decision = report.for_po("PO-7712")

    assert decision.exposure_days == expected
    assert expected == round(15.4 - 4.3, 1)  # 11.1 days resting on SUP-21's word


def test_monitor_reuses_a_supplied_coverage_report(store):
    """Passing a report the orchestrator already computed must not trigger a
    second pass."""
    coverage = compute_coverage(store, now=NOW)
    calls = {"n": 0}

    import app.engine.monitor as monitor_module

    original = monitor_module.compute_coverage

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monitor_module.compute_coverage = counting
    try:
        run_monitor_cycle(store, coverage=coverage, now=NOW)
        assert calls["n"] == 0
        run_monitor_cycle(store, now=NOW)
        assert calls["n"] == 1
    finally:
        monitor_module.compute_coverage = original


def test_monitor_does_not_define_dependable_inbound(store):
    """OPEN_ITEMS.md owes that definition to item 3. MONITOR must not shadow it
    — it reads coverage.py's target list and never judges PO dependability."""
    source = open("app/engine/monitor.py", encoding="utf-8").read()
    assert "DEPENDABLE_PO_STATUSES" not in source
    assert "dependable_inbound" not in source


def test_monitor_does_not_reimplement_coverage_math(store):
    """One owner for the coverage definitions."""
    source = open("app/engine/monitor.py", encoding="utf-8").read()
    assert "daily_usage" not in source
    assert "usable_stock" not in source
    assert "safety_stock" not in source


def test_monitor_does_not_touch_reliability(store):
    """VERIFY (item 3) owns reliability, on provenance grounds. MONITOR reports
    what tracking said and scores nobody for it."""
    source = open("app/engine/monitor.py", encoding="utf-8").read()
    assert "reliability_score" not in source

    # Behavioural, not just textual: a full cycle over the stalled PO-7712
    # leaves every supplier's score exactly where it was.
    before = {s.supplier_id: s.reliability_score for s in store.list_suppliers()}
    report = run_monitor_cycle(store, now=NOW)
    assert report.for_po("PO-7712").contradicts_po_status is True  # it did fire
    assert {s.supplier_id: s.reliability_score for s in store.list_suppliers()} == before


def test_event_ids_do_not_collide_with_coverage_events(store):
    """Both emitters draw from one sequence — duplicate ids would corrupt the
    provenance graph (item 7)."""
    store.purchase_orders["PO-7712"].status = "delayed"  # make coverage emit too
    coverage = compute_coverage(store, now=NOW)
    report = run_monitor_cycle(store, coverage=coverage, now=NOW)

    ids = [e.event_id for e in coverage.events] + [e.event_id for e in report.events]
    assert len(ids) == len(set(ids))


# ==========================================================================
# Invariants — ARCHITECTURE.md §8
# ==========================================================================


def test_a_poll_cycle_does_not_mutate_the_store(store):
    before = {
        "pos": [p.model_dump(mode="json") for p in store.list_purchase_orders()],
        "inventory": [r.model_dump(mode="json") for r in store.list_inventory()],
        "tracking": {k: v.model_dump(mode="json") for k, v in store.tracking.items()},
        "erp_log": len(store.erp_log),
    }

    run_monitor_cycle(store, now=NOW)
    run_monitor_cycle(store, now=NOW)

    assert [p.model_dump(mode="json") for p in store.list_purchase_orders()] == before["pos"]
    assert [r.model_dump(mode="json") for r in store.list_inventory()] == before["inventory"]
    assert {k: v.model_dump(mode="json") for k, v in store.tracking.items()} == before["tracking"]
    assert len(store.erp_log) == before["erp_log"]


def test_clean_slate_invariant_survives_a_poll_cycle():
    """ARCHITECTURE.md §8. A poll cycle must leave no residue that a later
    build_store() inherits — otherwise the judge panel (Beat 5) cannot reset
    between injections."""
    clock.reset()
    first = build_store()
    run_monitor_cycle(first, now=NOW)
    # Mutate hard after polling, the way an injected disruption would.
    first.purchase_orders["PO-7712"].status = "delayed"
    first.tracking["PO-7712"].tracking_status = "delivered"
    run_monitor_cycle(first, now=NOW)

    second = build_store()
    assert second.purchase_orders["PO-7712"].status == "in_transit"
    assert second.tracking["PO-7712"].tracking_status == "label_created_no_pickup"
    assert second.inventory["COMP-104"].usable_stock == 390
    assert first.purchase_orders["PO-7712"] is not second.purchase_orders["PO-7712"]

    # And the fresh store polls exactly as it did the first time.
    report = run_monitor_cycle(second, now=NOW)
    assert report.for_po("PO-7712").contradicts_po_status is True


def test_repeated_cycles_agree(store):
    first = run_monitor_cycle(store, now=NOW)
    second = run_monitor_cycle(store, now=NOW)

    assert [d.model_dump(mode="json") for d in first.decisions] == [
        d.model_dump(mode="json") for d in second.decisions
    ]


# ==========================================================================
# Contradiction rules
# ==========================================================================


def test_a_pending_po_with_not_shipped_tracking_is_not_a_contradiction(store):
    """A pending PO is not claiming to have shipped, so `not_shipped` agrees
    with it. Flagging that would be noise, and noise is what Tool Efficiency
    penalises."""
    assert "pending" not in IN_FLIGHT_PO_STATUSES

    report = run_monitor_cycle(store, now=NOW)
    for decision in report.polled():
        if decision.po_status == "pending":
            assert decision.contradicts_po_status is False


def test_an_already_delayed_po_is_not_re_flagged(store):
    """Once the PO record says delayed, tracking showing no pickup agrees with
    it. There is no new information, so there is no event."""
    store.purchase_orders["PO-7712"].status = "delayed"
    report = run_monitor_cycle(store, now=NOW)

    decision = report.for_po("PO-7712")
    if decision is not None and decision.polled:
        assert decision.contradicts_po_status is False
    assert not [e for e in report.events if e.po_id == "PO-7712"]


def test_tracking_statuses_come_from_the_spec():
    """§5.10 gives label_created_no_pickup verbatim as the contradiction case."""
    assert "label_created_no_pickup" in NOT_MOVED_TRACKING_STATUSES


def test_no_llm_is_reachable_from_monitor():
    """AGENTS.md rules 1 and 2."""
    source = open("app/engine/monitor.py", encoding="utf-8").read()
    assert "app.llm" not in source
    assert "genai" not in source
    assert "gemini" not in source.lower()


def test_zero_usage_component_reports_infinite_exposure_without_crashing(store):
    store.inventory["COMP-104"].daily_usage = 0
    report = run_monitor_cycle(store, now=NOW)

    for decision in report.decisions:
        if decision.component_id == "COMP-104":
            assert not math.isnan(decision.exposure_days)
