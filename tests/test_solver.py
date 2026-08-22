"""SOLVER tests — ARCHITECTURE.md §4 item 4.

The two hidden tests item 4 owns (ARCHITECTURE.md §10) are the tests that
matter most here:
  - "Cheapest supplier fails quality" — SUP-18 (cheapest, fastest, but
    uncertified) must be dropped by the cert filter before any RFQ call.
  - "Low-reliability supplier is fastest" — once VERIFY (item 3) downgrades
    SUP-21 after the PO-7712 contradiction, it is both the fastest COMP-104
    option AND the least reliable. Pareto must surface it, not silently drop
    it or average it into a single score.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.solver import (
    Rejection,
    SourcingCombination,
    _dominates,
    _is_certified,
    fetch_valid_quotes,
    hard_filter,
    pareto_front,
    run_solver,
)
from app.engine.verify import run_verification_cycle
from app.environment.clock import clock
from app.environment.schemas import RFQQuote
from app.environment.seed_data import build_store

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    return build_store()


def _downgrade_sup21_via_verify(store):
    """PROJECT.md §4 Beat 3's mechanic, reused: elicit SUP-21's 'dispatched'
    claim, run the real pipeline (coverage -> monitor -> verify) so
    reliability_score moves via the actual exponentially weighted reliability
    update, not a hand-set test value."""
    store.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Status check on PO-7712",
        body="Any update on PO-7712?",
    )
    coverage = compute_coverage(store, now=NOW)
    monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
    run_verification_cycle(store, coverage=coverage, monitor=monitor, now=NOW)


# ==========================================================================
# Hidden test #1 — cheapest supplier fails quality (cert filter, pre-RFQ)
# ==========================================================================


def test_sup18_is_cheapest_and_fastest_but_uncertified(store):
    """The precondition that makes this a real test, not a vacuous one."""
    sup18 = store.suppliers["SUP-18"]
    others = [s for s in store.list_suppliers(component_id="COMP-104") if s.supplier_id != "SUP-18"]

    assert sup18.unit_price < min(o.unit_price for o in others)
    assert sup18.lead_time_days <= min(o.lead_time_days for o in others)
    assert sup18.certifications == []


def test_sup18_is_dropped_before_any_rfq_call(store):
    """The cert filter must catch it using GET /suppliers data alone — no
    RFQ call spent proving what was already knowable."""
    survivors, rejections = hard_filter(store, "COMP-104")

    assert "SUP-18" not in [s.supplier_id for s in survivors]
    rejection = next(r for r in rejections if r.subject == "SUP-18")
    assert rejection.reason == "uncertified"

    assert len(store.rfq_log) == 0  # no RFQ tool call has happened yet


def test_sup18_never_reaches_the_pareto_set(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)

    for combo in result.pareto_set:
        assert "SUP-18" not in combo.supplier_ids
    assert any(r.subject == "SUP-18" and r.reason == "uncertified" for r in result.rejected)


def test_certified_means_holding_at_least_one_certification():
    from app.environment.schemas import SupplierRecord

    certified = SupplierRecord(
        supplier_id="X", supplier_name="x", component_id="COMP-104", unit_price=1,
        lead_time_days=1, available_quantity=1, quality_score=0.5, reliability_score=0.5,
        min_order_quantity=1, certifications=["ISO-9001"],
    )
    uncertified = certified.model_copy(update={"certifications": []})

    assert _is_certified(certified) is True
    assert _is_certified(uncertified) is False


# ==========================================================================
# Hidden test #2 — low-reliability supplier is fastest (Pareto, not collapsed)
# ==========================================================================


def test_sup21_is_fastest_among_certified_comp104_suppliers(store):
    certified = [s for s in store.list_suppliers(component_id="COMP-104") if s.certifications]
    fastest = min(certified, key=lambda s: s.lead_time_days)
    assert fastest.supplier_id == "SUP-21"


def test_verification_makes_sup21_the_least_reliable_certified_supplier(store):
    """The precondition: after the real PO-7712 contradiction moves SUP-21's
    score via item 3's actual reliability update, it must be lower than
    every other certified COMP-104 supplier — otherwise this isn't the
    scenario the hidden test describes."""
    before = store.suppliers["SUP-21"].reliability_score
    _downgrade_sup21_via_verify(store)
    after = store.suppliers["SUP-21"].reliability_score

    assert after < before
    others = [
        s.reliability_score
        for s in store.list_suppliers(component_id="COMP-104")
        if s.certifications and s.supplier_id != "SUP-21"
    ]
    assert after < min(others)


def test_low_reliability_fastest_supplier_survives_onto_the_pareto_front(store):
    """The hidden test itself: SUP-21, fast but now the least reliable
    certified option, must appear on the non-dominated set."""
    _downgrade_sup21_via_verify(store)

    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)

    sup21_combo = next(
        (c for c in result.pareto_set if c.supplier_ids == ["SUP-21"]), None
    )
    assert sup21_combo is not None, "SUP-21 was dropped or dominated, not surfaced"
    assert sup21_combo.reliability_score == store.suppliers["SUP-21"].reliability_score
    assert sup21_combo.reliability_score < 0.75  # confirms the live, post-verify value


def test_the_pareto_front_holds_more_than_one_option_when_the_tradeoff_is_real(store):
    """If Pareto collapsed to a single "best" option here, it would mean a
    weighted score picked a winner — exactly the rejected Trust Gate
    mechanism this module must not reintroduce."""
    _downgrade_sup21_via_verify(store)
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)

    assert len(result.pareto_set) >= 2


def test_reliability_is_read_live_from_the_store_not_a_fresh_default(store):
    """The mechanism behind the hidden test, checked directly: mutate
    reliability_score by hand (standing in for whatever produced it) and
    confirm the solver's output reflects that exact live value."""
    store.suppliers["SUP-21"].reliability_score = 0.11

    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    sup21_combo = next(c for c in result.pareto_set if c.supplier_ids == ["SUP-21"])

    assert sup21_combo.reliability_score == 0.11


# ==========================================================================
# No weighted-sum collapse
# ==========================================================================


def test_dominates_compares_six_axes_independently():
    """Structural proof: a combination cheaper AND slower than another must
    NOT dominate it — a weighted-sum score could easily let a big price win
    paper over a lead-time loss; six independent axis comparisons cannot."""
    cheaper_but_slower = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=1000.0, lead_time_days=10, reliability_score=0.9,
        quality_score=0.9, total_min_order_quantity=50, total_available_quantity=200,
    )
    pricier_but_faster = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=2000.0, lead_time_days=2, reliability_score=0.9,
        quality_score=0.9, total_min_order_quantity=50, total_available_quantity=200,
    )
    assert _dominates(cheaper_but_slower, pricier_but_faster) is False
    assert _dominates(pricier_but_faster, cheaper_but_slower) is False


def test_a_strictly_worse_combination_on_every_axis_is_dominated():
    better = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=1000.0, lead_time_days=3, reliability_score=0.9,
        quality_score=0.9, total_min_order_quantity=50, total_available_quantity=500,
    )
    worse = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=1500.0, lead_time_days=5, reliability_score=0.7,
        quality_score=0.8, total_min_order_quantity=80, total_available_quantity=300,
    )
    assert _dominates(better, worse) is True
    assert _dominates(worse, better) is False


def test_pareto_front_drops_the_dominated_one_with_a_named_reason():
    better = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=1000.0, lead_time_days=3, reliability_score=0.9,
        quality_score=0.9, total_min_order_quantity=50, total_available_quantity=500,
    )
    worse = SourcingCombination(
        members=[], quantity_needed=100, quantity_allocated=100,
        total_price=1500.0, lead_time_days=5, reliability_score=0.7,
        quality_score=0.8, total_min_order_quantity=80, total_available_quantity=300,
    )
    front, dominated = pareto_front([better, worse])

    assert better in front
    assert worse not in front
    assert len(dominated) == 1
    assert dominated[0].reason == "dominated"
    assert "price" in dominated[0].note


def test_no_scalar_score_is_computed_anywhere_in_solver(store):
    """The rejected Trust Gate's actual mistake was a weighted SUM across
    dimensions collapsed into one comparable number. Checked against the
    shipped source: no line multiplies a weight into price/lead_time/
    reliability/quality and adds the results together."""
    source = open("app/engine/solver.py", encoding="utf-8").read()
    banned = ["score =", "weighted_sum", "* weight", "weight *", "confidence_score"]
    lowered = source.lower()
    for term in banned:
        assert term.lower() not in lowered


# ==========================================================================
# Budget-infeasible filter
# ==========================================================================


def test_a_supplier_whose_minimum_order_alone_exceeds_the_threshold_is_dropped(store):
    """Minimum viable commitment, not full-quantity price — see the module
    docstring for why: exceeding the threshold for the FULL quantity is an
    escalation question (RATCHET), not a hard-filter question."""
    supplier = store.suppliers["SUP-42"]
    supplier.unit_price = 1000.0
    supplier.min_order_quantity = 200  # 200,000 > 150,000 default threshold

    survivors, rejections = hard_filter(store, "COMP-104")

    assert "SUP-42" not in [s.supplier_id for s in survivors]
    rejection = next(r for r in rejections if r.subject == "SUP-42")
    assert rejection.reason == "budget_infeasible"
    assert rejection.estimated_total_price == 200_000.0


def test_a_supplier_whose_full_quantity_price_is_high_but_moq_is_affordable_survives(store):
    """The distinction that keeps order-splitting possible: expensive-at-full
    -volume is not the same as infeasible-at-minimum-order."""
    supplier = store.suppliers["SUP-37"]
    # min order (200 @ 126) = 25,200 — comfortably under threshold — but
    # 900 units @ 126 = 113,400, and a caller wanting MORE than that from
    # SUP-37 alone could still trip escalation on the full quantity. The
    # hard filter must not pre-empt that; it only checks the MOQ commitment.
    survivors, rejections = hard_filter(store, "COMP-104")
    assert "SUP-37" in [s.supplier_id for s in survivors]
    assert not [r for r in rejections if r.subject == "SUP-37"]


def test_budget_filter_reuses_check_approval_not_a_reimplemented_threshold(store):
    """The threshold-selection rule (per-PO override vs global default) must
    come from store.check_approval (§5.8), not a second copy of that logic."""
    source = open("app/engine/solver.py", encoding="utf-8").read()
    assert "check_approval" in source
    assert "TRACE_APPROVAL_THRESHOLD" not in source


# ==========================================================================
# quote_valid_hours — enforced, not displayed
# ==========================================================================


def test_a_fresh_quote_is_valid(store):
    survivors, _ = hard_filter(store, "COMP-104")
    quotes, rejections, requested, reused = fetch_valid_quotes(
        store, survivors, 900, now=NOW
    )
    assert len(quotes) == len(survivors)
    assert not rejections
    assert requested == len(survivors)
    assert reused == 0


def test_a_quote_past_its_validity_window_is_dropped_like_a_cert_failure(store):
    survivors, _ = hard_filter(store, "COMP-104")
    quotes, _, _, _ = fetch_valid_quotes(store, survivors, 900, now=NOW)

    later = NOW + timedelta(hours=7)  # quote_valid_hours is 6
    result = run_solver(
        store,
        component_id="COMP-104",
        quantity_needed=900,
        existing_quotes=quotes,
        now=later,
    )

    expired_subjects = {r.subject for r in result.rejected if r.reason == "expired_quote"}
    assert expired_subjects == {s.supplier_id for s in survivors}
    assert result.pareto_set == []  # nothing left to combine
    assert result.quotes_requested == 0  # not silently auto-refreshed


def test_a_quote_within_its_validity_window_is_reused_not_refetched(store):
    survivors, _ = hard_filter(store, "COMP-104")
    quotes, _, _, _ = fetch_valid_quotes(store, survivors, 900, now=NOW)

    just_before_expiry = NOW + timedelta(hours=5, minutes=59)
    result = run_solver(
        store,
        component_id="COMP-104",
        quantity_needed=900,
        existing_quotes=quotes,
        now=just_before_expiry,
    )

    assert result.quotes_requested == 0
    assert result.quotes_reused == len(survivors)
    assert len(result.pareto_set) > 0


def test_expiry_check_uses_quote_issued_at_plus_quote_valid_hours_exactly():
    quote = RFQQuote(
        supplier_id="SUP-21", component_id="COMP-104", quantity_available=900,
        unit_price=118.0, delivery_days=3, expedite_available=True, expedite_fee=0.0,
        quote_valid_hours=6, quote_issued_at=NOW,
    )
    from app.engine.solver import _is_quote_valid

    assert _is_quote_valid(quote, NOW + timedelta(hours=6)) is True  # inclusive boundary
    assert _is_quote_valid(quote, NOW + timedelta(hours=6, seconds=1)) is False


# ==========================================================================
# Tool efficiency — one RFQ call per survivor, never a broad sweep
# ==========================================================================


def test_rfq_is_never_requested_for_a_hard_filtered_supplier(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    for rfq_quote in store.rfq_log:
        assert rfq_quote.supplier_id != "SUP-18"


def test_exactly_one_rfq_call_per_surviving_supplier(store):
    survivors, _ = hard_filter(store, "COMP-104")
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    assert result.quotes_requested == len(survivors)


# ==========================================================================
# Combination enumeration — brute force, ≤2 suppliers
# ==========================================================================


def test_a_split_covers_at_least_the_quantity_needed(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    for combo in result.pareto_set:
        assert combo.quantity_allocated >= combo.quantity_needed


def test_a_split_member_never_contributes_below_its_own_moq(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    for combo in result.pareto_set:
        for member in combo.members:
            assert member.quantity >= member.min_order_quantity


def test_single_supplier_option_is_not_duplicated_as_a_two_way_split(store):
    """If SUP-21 alone covers everything, (SUP-21, SUP-37) must not also
    appear as a redundant combination where SUP-37 contributes zero."""
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    for combo in result.pareto_set:
        assert len(combo.members) == len(set(combo.supplier_ids))
        for member in combo.members:
            assert member.quantity > 0


# ==========================================================================
# Output feeds item 5 — rejected alternatives, reasons, no invented "saved"
# ==========================================================================


def test_every_rejection_carries_a_named_reason(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    valid_reasons = {"uncertified", "budget_infeasible", "expired_quote", "dominated"}
    for rejection in result.rejected:
        assert rejection.reason in valid_reasons


def test_solver_does_not_compute_a_dollar_saved_figure():
    """That requires knowing the FINAL selected plan — item 5/6's decision,
    not this module's. Checked structurally: Rejection has no 'saved' field."""
    assert "saved" not in Rejection.model_fields


def test_rejections_carry_the_comparable_figures_item_5_needs(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    uncertified = next(r for r in result.rejected if r.reason == "uncertified")
    assert uncertified.estimated_unit_price is not None
    assert uncertified.lead_time_days is not None
    assert uncertified.reliability_score is not None
    assert uncertified.quality_score is not None


# ==========================================================================
# Invariants — ARCHITECTURE.md §8, AGENTS.md rules
# ==========================================================================


def test_solver_does_not_mutate_suppliers_inventory_or_purchase_orders(store):
    before = {
        "suppliers": [s.model_dump(mode="json") for s in store.list_suppliers()],
        "inventory": [i.model_dump(mode="json") for i in store.list_inventory()],
        "pos": [p.model_dump(mode="json") for p in store.list_purchase_orders()],
        "erp_log": len(store.erp_log),
    }

    run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)

    assert [s.model_dump(mode="json") for s in store.list_suppliers()] == before["suppliers"]
    assert [i.model_dump(mode="json") for i in store.list_inventory()] == before["inventory"]
    assert [p.model_dump(mode="json") for p in store.list_purchase_orders()] == before["pos"]
    assert len(store.erp_log) == before["erp_log"]


def test_clean_slate_invariant_survives_a_solver_pass():
    """ARCHITECTURE.md §8: a solver pass calls request_rfq (which does mutate
    store.rfq_log — a legitimate, expected side effect of that tool), but
    must not leave anything that leaks into the next build_store()."""
    clock.reset()
    first = build_store()
    run_solver(first, component_id="COMP-104", quantity_needed=900, now=NOW)
    assert len(first.rfq_log) > 0  # confirms the pass actually ran

    second = build_store()
    assert len(second.rfq_log) == 0
    assert second.suppliers["SUP-21"] is not first.suppliers["SUP-21"]


def test_repeated_solves_reusing_the_same_quotes_agree_exactly(store):
    """Given FIXED quotes, filtering + combination + Pareto is fully
    deterministic — checked by reusing one quote set across two solves."""
    survivors, _ = hard_filter(store, "COMP-104")
    quotes, _, _, _ = fetch_valid_quotes(store, survivors, 900, now=NOW)

    first = run_solver(
        store, component_id="COMP-104", quantity_needed=900, existing_quotes=quotes, now=NOW
    )
    second = run_solver(
        store, component_id="COMP-104", quantity_needed=900, existing_quotes=quotes, now=NOW
    )

    first_prices = sorted(c.total_price for c in first.pareto_set)
    second_prices = sorted(c.total_price for c in second.pareto_set)
    assert first_prices == second_prices
    assert first.quotes_requested == 0
    assert second.quotes_requested == 0


def test_two_independent_solves_without_reusing_quotes_legitimately_differ_in_price(store):
    """NOT a bug: seed_data.py's request_rfq applies fresh random jitter on
    every call ("real RFQ desks quote fresh," its own docstring) — each solve
    here issues its own RFQ, drawing further from the store's seeded RNG.
    The STRUCTURE (who's eligible, who's rejected and why) must stay stable
    even though the exact quoted price moves."""
    first = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)
    second = run_solver(store, component_id="COMP-104", quantity_needed=900, now=NOW)

    first_suppliers = sorted(set().union(*[set(c.supplier_ids) for c in first.pareto_set]))
    second_suppliers = sorted(set().union(*[set(c.supplier_ids) for c in second.pareto_set]))
    assert first_suppliers == second_suppliers

    first_reasons = sorted((r.subject, r.reason) for r in first.rejected if r.reason != "dominated")
    second_reasons = sorted((r.subject, r.reason) for r in second.rejected if r.reason != "dominated")
    assert first_reasons == second_reasons


def test_no_llm_is_reachable_from_solver():
    """AGENTS.md rules 1 and 2 — coverage math, filtering, and comparison are
    deterministic; nothing here should import an LLM module."""
    source = open("app/engine/solver.py", encoding="utf-8").read()
    assert "app.llm" not in source
    assert "genai" not in source
    assert "gemini" not in source.lower()


def test_zero_quantity_needed_does_not_crash(store):
    result = run_solver(store, component_id="COMP-104", quantity_needed=0, now=NOW)
    assert isinstance(result.pareto_set, list)


def test_unknown_component_returns_no_candidates_not_an_error(store):
    result = run_solver(store, component_id="COMP-999-DOES-NOT-EXIST", quantity_needed=100, now=NOW)
    assert result.pareto_set == []
    assert result.rejected == []
