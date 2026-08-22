"""PLANNER tests — ARCHITECTURE.md §4 item 5.

The live-state smoke test at the bottom (`test_smoke_against_the_real_current_state`)
is run against ACTUAL post-verify pipeline state, not hand-picked numbers, and
documents whatever the real output is — including where it diverges from
PROJECT.md §4 Beat 4's promised "delay PROD-914 by two days." It does not.
Reported honestly per the task's own instruction, not forced to match.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from app.engine.coverage import CoverageResult, compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import (
    COST_OF_INACTION_NOTE,
    SELECTION_RULE,
    _select_combination,
    _sort_orders,
    allocate_stock,
    next_plan_id,
    reset_plan_sequence,
    run_planner,
)
from app.engine.solver import (
    Rejection,
    SolverResult,
    SourcingCombination,
    SupplierOption,
    run_solver,
)
from app.engine.verify import run_verification_cycle
from app.environment.clock import clock
from app.environment.seed_data import build_store

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    reset_plan_sequence()
    return build_store()


def _combo(
    supplier_id: str,
    price: float,
    lead: int,
    reliability: float,
    quality: float = 0.9,
    qty: int = 500,
    moq: int = 100,
    avail: int = 500,
) -> SourcingCombination:
    option = SupplierOption(
        supplier_id=supplier_id,
        unit_price=price / qty,
        quantity=qty,
        available_quantity=avail,
        lead_time_days=lead,
        reliability_score=reliability,
        quality_score=quality,
        min_order_quantity=moq,
    )
    return SourcingCombination(
        members=[option],
        quantity_needed=qty,
        quantity_allocated=qty,
        total_price=price,
        lead_time_days=lead,
        reliability_score=reliability,
        quality_score=quality,
        total_min_order_quantity=moq,
        total_available_quantity=avail,
    )


def _coverage_result(
    production_order_id: str,
    priority: str,
    days_to_deadline: float,
    component_required: int,
    component_id: str = "COMP-104",
) -> CoverageResult:
    return CoverageResult(
        production_order_id=production_order_id,
        product="x",
        component_id=component_id,
        component_name="x",
        days_of_coverage_on_hand=0.0,
        days_of_coverage=0.0,
        # Derived from days_to_deadline, not a fixed literal -- allocate_stock
        # and _sort_orders both key off days_to_deadline's own consistency
        # with deadline (same relationship compute_coverage itself keeps for
        # a fixed `now`), so test fixtures must keep the two in sync too.
        deadline=NOW + timedelta(days=days_to_deadline),
        priority=priority,
        days_to_deadline=days_to_deadline,
        status="critical",
        is_thin=True,
        reason="stockout_before_deadline",
        safety_stock=0,
        days_to_safety_breach=math.inf,
        component_required=component_required,
        usable_stock=0,
        daily_usage=90,
    )


def _elicit_and_verify(store, now=NOW):
    store.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Status check on PO-7712",
        body="Any update on PO-7712?",
    )
    coverage = compute_coverage(store, now=now)
    monitor = run_monitor_cycle(store, coverage=coverage, now=now)
    run_verification_cycle(store, coverage=coverage, monitor=monitor, now=now)


# ==========================================================================
# Selection rule — the sentence and the code must match
# ==========================================================================


def test_selection_rule_sentence_names_its_own_three_tiers():
    assert "reliability_score" in SELECTION_RULE
    assert "lead_time_days" in SELECTION_RULE
    assert "total_price" in SELECTION_RULE
    # order of appearance in the sentence == order of precedence
    assert SELECTION_RULE.index("reliability_score") < SELECTION_RULE.index("lead_time_days")
    assert SELECTION_RULE.index("lead_time_days") < SELECTION_RULE.index("total_price")


def test_reliability_is_the_primary_tier_even_against_a_large_lead_time_gap():
    slow_but_reliable = _combo("A", price=1000, lead=10, reliability=0.9)
    fast_but_unreliable = _combo("B", price=1000, lead=1, reliability=0.5)
    chosen = _select_combination([slow_but_reliable, fast_but_unreliable])
    assert chosen is slow_but_reliable


def test_lead_time_breaks_a_reliability_tie():
    slow = _combo("A", price=1000, lead=10, reliability=0.8)
    fast = _combo("B", price=1000, lead=2, reliability=0.8)
    chosen = _select_combination([slow, fast])
    assert chosen is fast


def test_price_breaks_a_reliability_and_lead_time_tie():
    pricier = _combo("A", price=2000, lead=5, reliability=0.8)
    cheaper = _combo("B", price=1000, lead=5, reliability=0.8)
    chosen = _select_combination([pricier, cheaper])
    assert chosen is cheaper


def test_selection_is_never_a_weighted_sum():
    """Structural check: no line combines the three axes into one number."""
    source = open("app/engine/planner.py", encoding="utf-8").read()
    lowered = source.lower()
    for banned in ["score =", "weighted_sum", "* weight", "weight *"]:
        assert banned not in lowered


# ==========================================================================
# Stock allocation — priority then deadline, no invented weight
# ==========================================================================


def test_orders_are_served_high_priority_first_regardless_of_list_order():
    low = _coverage_result("PROD-A", "low", days_to_deadline=2, component_required=100)
    high = _coverage_result("PROD-B", "high", days_to_deadline=20, component_required=100)
    ordered = _sort_orders([low, high])
    assert [o.production_order_id for o in ordered] == ["PROD-B", "PROD-A"]


def test_same_priority_orders_are_served_by_earliest_deadline_first():
    later = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    sooner = _coverage_result("PROD-B", "high", days_to_deadline=3, component_required=100)
    ordered = _sort_orders([later, sooner])
    assert [o.production_order_id for o in ordered] == ["PROD-B", "PROD-A"]


def test_on_hand_stock_goes_to_the_higher_priority_order_first():
    high = _coverage_result("PROD-HIGH", "high", days_to_deadline=5, component_required=300)
    low = _coverage_result("PROD-LOW", "low", days_to_deadline=20, component_required=300)
    combo = _combo("X", price=1000, lead=5, reliability=0.8, qty=200, moq=50, avail=200)

    details = allocate_stock([low, high], on_hand=200, combination=combo)
    by_id = {d.production_order_id: d for d in details}

    assert by_id["PROD-HIGH"].allocated_on_hand == 200
    assert by_id["PROD-LOW"].allocated_on_hand == 0


def test_incoming_supply_is_allocated_fastest_arriving_member_first():
    """A 2-way split has two arrival times; the faster one should reach the
    higher-priority order's shortfall before the slower one does."""
    high = _coverage_result("PROD-HIGH", "high", days_to_deadline=10, component_required=100)

    fast = SupplierOption(
        supplier_id="FAST", unit_price=10, quantity=100, available_quantity=100,
        lead_time_days=2, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    slow = SupplierOption(
        supplier_id="SLOW", unit_price=10, quantity=100, available_quantity=100,
        lead_time_days=8, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    combo = SourcingCombination(
        members=[slow, fast],  # deliberately out of order — sorting is allocate_stock's job
        quantity_needed=100, quantity_allocated=200, total_price=2000,
        lead_time_days=8, reliability_score=0.8, quality_score=0.8,
        total_min_order_quantity=20, total_available_quantity=200,
    )

    details = allocate_stock([high], on_hand=0, combination=combo)
    assert details[0].earliest_full_supply_day == 2.0  # satisfied entirely by FAST


def test_a_shortfall_that_needs_the_slower_member_reflects_that_arrival_day():
    high = _coverage_result("PROD-HIGH", "high", days_to_deadline=10, component_required=150)
    fast = SupplierOption(
        supplier_id="FAST", unit_price=10, quantity=100, available_quantity=100,
        lead_time_days=2, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    slow = SupplierOption(
        supplier_id="SLOW", unit_price=10, quantity=100, available_quantity=100,
        lead_time_days=8, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    combo = SourcingCombination(
        members=[fast, slow], quantity_needed=150, quantity_allocated=200, total_price=2000,
        lead_time_days=8, reliability_score=0.8, quality_score=0.8,
        total_min_order_quantity=20, total_available_quantity=200,
    )
    details = allocate_stock([high], on_hand=0, combination=combo)
    # 100 from FAST (day 2) isn't enough alone (needs 150) -> draws 50 more
    # from SLOW, so it isn't done until day 8.
    assert details[0].allocated_incoming == 150
    assert details[0].earliest_full_supply_day == 8.0


# ==========================================================================
# Production reschedule — computed from the actual shortfall
# ==========================================================================


def test_an_order_that_is_supplied_before_its_deadline_gets_no_reschedule(store):
    on_time = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)

    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    plan = run_planner(store, "COMP-X", result, [on_time], now=NOW)
    assert plan.reschedule_actions() == []


def test_an_order_supplied_after_its_deadline_gets_a_reschedule_with_a_computed_delay(store):
    """delay_days must come from the real gap between arrival and deadline,
    not a hardcoded figure."""
    tight = _coverage_result("PROD-TIGHT", "low", days_to_deadline=3.0, component_required=100)
    combo = _combo("X", price=1000, lead=9, reliability=0.8, qty=100, moq=10, avail=100)

    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    plan = run_planner(store, "COMP-X", result, [tight], now=NOW)

    reschedules = plan.reschedule_actions()
    assert len(reschedules) == 1
    action = reschedules[0]
    assert action.production_order_id == "PROD-TIGHT"
    # earliest_full_supply_day=9, days_to_deadline=3.0 -> ceil(9-3.0) = 6
    assert action.delay_days == 6
    assert action.reversibility == "reversible"
    assert "PROD-TIGHT" in action.justification
    assert "6" in action.justification


def test_delay_days_scales_with_the_actual_gap_not_a_fixed_number(store):
    """Two different shortfalls must produce two different delay_days —
    proof the figure is computed, not a constant."""
    tight_a = _coverage_result("PROD-A", "low", days_to_deadline=3.0, component_required=100)
    tight_b = _coverage_result("PROD-B", "low", days_to_deadline=3.0, component_required=100)
    combo_small_gap = _combo("X", price=1000, lead=4, reliability=0.8, qty=100, moq=10, avail=100)
    combo_big_gap = _combo("Y", price=1000, lead=15, reliability=0.8, qty=100, moq=10, avail=100)

    result_small = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo_small_gap], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    result_big = SolverResult(
        computed_at=NOW, component_id="COMP-Y", quantity_needed=100,
        pareto_set=[combo_big_gap], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    plan_small = run_planner(store, "COMP-X", result_small, [tight_a], now=NOW)
    plan_big = run_planner(store, "COMP-Y", result_big, [tight_b], now=NOW)

    assert plan_small.reschedule_actions()[0].delay_days == 1  # ceil(4-3.0)
    assert plan_big.reschedule_actions()[0].delay_days == 12  # ceil(15-3.0)
    assert plan_small.reschedule_actions()[0].delay_days != plan_big.reschedule_actions()[0].delay_days


def test_reschedule_can_fall_on_the_higher_priority_order_if_the_numbers_say_so(store):
    """No special-casing "only reschedule low priority" — whichever order the
    real allocation says is short gets the action, high priority or not."""
    urgent_but_underserved = _coverage_result(
        "PROD-URGENT", "high", days_to_deadline=2.0, component_required=200
    )
    patient = _coverage_result("PROD-PATIENT", "low", days_to_deadline=30.0, component_required=50)

    fast_small = SupplierOption(
        supplier_id="FAST", unit_price=10, quantity=50, available_quantity=50,
        lead_time_days=1, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    slow_big = SupplierOption(
        supplier_id="SLOW", unit_price=10, quantity=200, available_quantity=200,
        lead_time_days=10, reliability_score=0.8, quality_score=0.8, min_order_quantity=10,
    )
    combo = SourcingCombination(
        members=[fast_small, slow_big], quantity_needed=250, quantity_allocated=250,
        total_price=2500, lead_time_days=10, reliability_score=0.8, quality_score=0.8,
        total_min_order_quantity=20, total_available_quantity=250,
    )
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=250,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    # PROD-URGENT (high priority) is served first, exhausts FAST (50) and
    # still needs 150 more from SLOW (day 10) -- misses its 2-day deadline.
    plan = run_planner(store, "COMP-X", result, [patient, urgent_but_underserved], now=NOW)

    reschedules = {a.production_order_id: a for a in plan.reschedule_actions()}
    assert "PROD-URGENT" in reschedules
    assert "PROD-PATIENT" not in reschedules


# ==========================================================================
# Reversibility tagging — AGENTS.md rule 5
# ==========================================================================


def test_purchase_split_is_tagged_compensable_never_irreversible(store):
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    for action in plan.purchase_actions():
        assert action.reversibility == "compensable"


def test_no_action_this_module_produces_is_ever_tagged_irreversible(store):
    """AGENTS.md rule 5: only POST /erp/update is irreversible, and it does
    not happen in this module."""
    combo = _combo("X", price=1000, lead=9, reliability=0.8, qty=100, moq=10, avail=100)
    tight = _coverage_result("PROD-TIGHT", "low", days_to_deadline=1.0, component_required=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    plan = run_planner(store, "COMP-X", result, [tight], now=NOW)

    reversibility_values = {a.reversibility for a in plan.actions}
    assert "irreversible" not in reversibility_values
    assert reversibility_values <= {"compensable", "reversible"}


def test_planner_never_calls_erp_update(store):
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    before = len(store.erp_log)
    run_planner(store, "COMP-X", result, [cov], now=NOW)
    assert len(store.erp_log) == before


# ==========================================================================
# total_cost — real quote data, never estimated
# ==========================================================================


def test_total_cost_equals_the_chosen_combinations_real_total_price(store):
    combo = _combo("X", price=54321.0, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    assert plan.total_cost == 54321.0
    assert plan.total_cost == plan.chosen_combination.total_price


def test_purchase_action_unit_prices_match_the_quote_not_a_recomputed_figure(store):
    combo = _combo("X", price=1000.0, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    action = plan.purchase_actions()[0]
    member = combo.members[0]
    assert action.unit_price == member.unit_price
    assert action.qty == member.quantity


# ==========================================================================
# cost_of_inaction — not invented
# ==========================================================================


def test_cost_of_inaction_is_none_not_a_guessed_number(store):
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    assert plan.cost_of_inaction is None
    assert plan.cost_of_inaction_note == COST_OF_INACTION_NOTE
    assert "not" in plan.cost_of_inaction_note.lower()


def test_no_numeric_cost_of_inaction_formula_exists_in_the_source():
    """The illustrative 340000 from ARCHITECTURE.md §7 may be discussed in
    prose (explaining why it's NOT used) but must never be assigned as a
    computed value anywhere in this module."""
    source = open("app/engine/planner.py", encoding="utf-8").read()
    assert "cost_of_inaction=340000" not in source.replace(" ", "")
    assert "cost_of_inaction = 340000" not in source
    assert "= 340000" not in source
    assert "=340000" not in source


def test_cost_of_inaction_note_is_a_reasoned_decision_not_a_one_line_shrug():
    """The note must show the search was real: what was checked, what the
    one concrete fallback formula needed and didn't have, and why two
    further fallbacks built from data that DOES exist were still rejected
    (circularity with total_cost; an invented time horizon). A one-liner
    here would fail the same "don't silently treat None as done" bar the
    field itself exists to satisfy."""
    note = COST_OF_INACTION_NOTE.lower()
    assert "committed sale price" in note or "committed" in note
    assert "procurement" in note
    assert "circular" in note
    assert "time horizon" in note
    assert "blocking" in note
    assert len(COST_OF_INACTION_NOTE) > 400  # not a boilerplate sentence


# ==========================================================================
# rejected_alternatives — solver's reasons carried forward, saved computed
# ==========================================================================


def test_solver_rejections_are_carried_forward_with_their_original_reason(store):
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    rejection = Rejection(
        subject="SUP-BAD", reason="uncertified",
        note="SUP-BAD holds no certifications.",
        estimated_unit_price=5.0,
    )
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[rejection], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    alt = next(a for a in plan.rejected_alternatives if a.option == "SUP-BAD")
    assert alt.reason == "uncertified"
    assert alt.regret == "SUP-BAD holds no certifications."  # verbatim, not re-derived
    # SUP-BAD's estimated cost (5.0 * 100 = 500) is cheaper than the chosen
    # plan (1000) -- saved = estimate - chosen = negative, a real regret.
    assert alt.saved == round(5.0 * 100 - 1000, 2)
    assert alt.saved < 0


def test_non_chosen_pareto_members_become_not_selected_alternatives(store):
    chosen_candidate = _combo("A", price=1000, lead=3, reliability=0.9, qty=100, moq=10, avail=100)
    loser = _combo("B", price=900, lead=3, reliability=0.5, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[chosen_candidate, loser], rejected=[], quotes_requested=2, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    assert plan.chosen_combination is chosen_candidate
    not_selected = [a for a in plan.rejected_alternatives if a.reason == "not_selected"]
    assert len(not_selected) == 1
    assert not_selected[0].option == loser.label
    assert "reliability_score" in not_selected[0].regret
    # loser (900) is cheaper than chosen (1000) -- saved = 900 - 1000 = -100,
    # a real regret (rejecting it did not save money, it cost 100 more).
    assert not_selected[0].saved == round(900 - 1000, 2)
    assert not_selected[0].saved < 0


def test_planner_does_not_re_derive_solver_rejection_reasons(store):
    """Structural: planner.py must not contain its own copy of solver's
    DropReason logic (cert/budget/expiry checks) -- it only reads Rejection
    objects solver already produced."""
    source = open("app/engine/planner.py", encoding="utf-8").read()
    assert "_is_certified" not in source
    assert "min_order_quantity * " not in source  # no re-derived budget math


# ==========================================================================
# Empty Pareto set
# ==========================================================================


def test_empty_pareto_set_returns_none_not_an_empty_plan(store):
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[], rejected=[], quotes_requested=0, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)
    assert plan is None


# ==========================================================================
# Invariants
# ==========================================================================


def test_planner_does_not_mutate_the_store(store):
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-104", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)

    before = {
        "inventory": [i.model_dump(mode="json") for i in store.list_inventory()],
        "suppliers": [s.model_dump(mode="json") for s in store.list_suppliers()],
        "pos": [p.model_dump(mode="json") for p in store.list_purchase_orders()],
        "erp_log": len(store.erp_log),
    }
    run_planner(store, "COMP-104", result, [cov], now=NOW)

    assert [i.model_dump(mode="json") for i in store.list_inventory()] == before["inventory"]
    assert [s.model_dump(mode="json") for s in store.list_suppliers()] == before["suppliers"]
    assert [p.model_dump(mode="json") for p in store.list_purchase_orders()] == before["pos"]
    assert len(store.erp_log) == before["erp_log"]


def test_no_llm_is_reachable_from_planner():
    source = open("app/engine/planner.py", encoding="utf-8").read()
    assert "app.llm" not in source
    assert "genai" not in source
    assert "gemini" not in source.lower()


def test_plan_ids_increment_and_reset():
    reset_plan_sequence()
    assert next_plan_id() == "PLAN-0001"
    assert next_plan_id() == "PLAN-0002"
    reset_plan_sequence()
    assert next_plan_id() == "PLAN-0001"


# ==========================================================================
# Smoke test — actual current pipeline state, real numbers, no forcing
# ==========================================================================


def test_smoke_against_the_real_current_state(store):
    """Runs the full pipeline (verify -> the Beat-2 PO-7712 delay -> solve ->
    plan) against ACTUAL current state and asserts on the REAL output.

    This does NOT assert "delay PROD-914 by two days" (PROJECT.md §4 Beat
    4's promised number) because that is not what the real numbers produce
    right now: both orders come out fully allocated on time under the
    reliability-first selection rule (SUP-37+SUP-42, chosen over SUP-21
    alone because SUP-21's post-verify reliability is lower), and the
    fastest-arrival-first allocation happens to land both PROD-882 and
    PROD-914 before their respective deadlines. Documented here so a future
    change that silently breaks this doesn't go unnoticed, and so the
    divergence from the demo script's promised number is a checked fact, not
    a claim.
    """
    _elicit_and_verify(store, now=NOW)
    assert store.suppliers["SUP-21"].reliability_score == 0.45

    store.purchase_orders["PO-7712"].status = "delayed"
    coverage = compute_coverage(store, now=NOW)
    comp104_results = [r for r in coverage.results if r.component_id == "COMP-104"]
    assert {r.production_order_id for r in comp104_results} == {"PROD-882", "PROD-914"}

    total_need = sum(r.component_required for r in comp104_results)
    assert total_need == 950  # 700 (PROD-882) + 250 (PROD-914)

    solver_result = run_solver(store, component_id="COMP-104", quantity_needed=total_need, now=NOW)
    plan = run_planner(store, "COMP-104", solver_result, comp104_results, now=NOW)

    assert plan is not None
    # The real selection: SUP-21 alone (price 110162, lead 3, reliability
    # 0.45) loses to the SUP-37+SUP-42 split on reliability, tier 1 of
    # SELECTION_RULE.
    assert set(plan.chosen_combination.supplier_ids) == {"SUP-37", "SUP-42"}
    assert plan.chosen_combination.reliability_score == 0.81

    # The real allocation outcome: no reschedule fires. Both orders are
    # fully covered before their own deadlines given fastest-arrival-first
    # allocation within the chosen split.
    assert plan.reschedule_actions() == []
    for detail in plan.allocations:
        assert detail.shortfall == 0
        assert detail.on_time is True

    # SUP-21 alone appears as a rejected, not-selected alternative — cheaper
    # (110162 vs 123674) but less reliable, exactly the "low-reliability
    # supplier is fastest" tension item 4 surfaced, now resolved by a
    # documented rule rather than silently discarded.
    sup21_alt = next(
        a for a in plan.rejected_alternatives if a.option == "SUP-21 only"
    )
    assert sup21_alt.reason == "not_selected"
    # SUP-21 (110162) was actually CHEAPER than the chosen split (123674), so
    # rejecting it saved nothing on cost -- saved is negative, a real regret,
    # not a saving. The narrower promise (reliability 0.45) is what it lost on.
    assert sup21_alt.saved == round(110162.0 - 123674.0, 2)
    assert sup21_alt.saved < 0
    assert "reliability_score" in sup21_alt.regret

    assert plan.cost_of_inaction is None


# ==========================================================================
# Adversarial — a confirmed, logged gap: nothing yet catches "this reschedule
# is too large to accept." Not fixed here; item 6 (RATCHET) is the
# architecturally correct place, per AGENTS.md rule 3's own named trigger
# "no supplier meets deadline -> escalate." See OPEN_ITEMS.md.
# ==========================================================================


def test_ADVERSARIAL_reliability_first_choice_can_silently_blow_a_deadline_reschedule_would_have_avoided(
    store,
):
    """Construct a Pareto set where SELECTION_RULE's own reliability-first
    tier picks a candidate whose delivery lands 15 days past a HIGH-priority
    order's 5-day deadline, while the REJECTED alternative (SUP-RISKY) would
    have made that same deadline comfortably (2 days).

    This is not a bug in allocate_stock or _reschedule_actions — delay_days
    is computed correctly (ceil(20 - 5) = 15) and the action is tagged
    correctly (reversible). The gap is architectural: nothing in this module
    or anywhere else in the pipeline judges whether a 15-day reschedule
    against a 5-day deadline is an ACCEPTABLE plan to hand back silently, or
    whether "no supplier meets deadline" (AGENTS.md rule 3's own named hard
    escalation trigger) should have fired instead of quietly rescheduling.
    `rejected_alternatives`' regret text for the faster option only cites the
    reliability_score it lost on — it never mentions that choosing it would
    have hit the deadline. Logged in OPEN_ITEMS.md as an explicit
    requirement for item 6.
    """
    reliable_but_glacial = SourcingCombination(
        members=[
            SupplierOption(
                supplier_id="SUP-RELIABLE", unit_price=100, quantity=100,
                available_quantity=100, lead_time_days=20, reliability_score=0.95,
                quality_score=0.9, min_order_quantity=10,
            )
        ],
        quantity_needed=100, quantity_allocated=100, total_price=10000,
        lead_time_days=20, reliability_score=0.95, quality_score=0.9,
        total_min_order_quantity=10, total_available_quantity=100,
    )
    fast_but_risky = SourcingCombination(
        members=[
            SupplierOption(
                supplier_id="SUP-RISKY", unit_price=90, quantity=100,
                available_quantity=100, lead_time_days=2, reliability_score=0.5,
                quality_score=0.8, min_order_quantity=10,
            )
        ],
        quantity_needed=100, quantity_allocated=100, total_price=9000,
        lead_time_days=2, reliability_score=0.5, quality_score=0.8,
        total_min_order_quantity=10, total_available_quantity=100,
    )

    result = SolverResult(
        computed_at=NOW, component_id="COMP-ADV", quantity_needed=100,
        pareto_set=[reliable_but_glacial, fast_but_risky], rejected=[],
        quotes_requested=2, quotes_reused=0,
    )
    urgent = _coverage_result(
        "PROD-URGENT", "high", days_to_deadline=5.0, component_required=100,
        component_id="COMP-ADV",
    )

    # Precondition: the rejected alternative genuinely would have made the
    # deadline. If this ever fails, the test above isn't proving anything.
    alt_allocation = allocate_stock([urgent], on_hand=0, combination=fast_but_risky)
    assert alt_allocation[0].on_time is True

    plan = run_planner(store, "COMP-ADV", result, [urgent], now=NOW)

    # SELECTION_RULE, exactly as documented and tested above, picks the
    # reliable-but-glacial candidate -- reliability (0.95) beats lead time
    # entirely, regardless of the deadline consequence.
    assert plan.chosen_combination.supplier_ids == ["SUP-RELIABLE"]

    # allocate_stock / _reschedule_actions compute the miss correctly and
    # honestly -- this is NOT where the gap is.
    detail = plan.allocations[0]
    assert detail.on_time is False
    assert detail.earliest_full_supply_day == 20.0

    reschedule = plan.reschedule_actions()[0]
    assert reschedule.production_order_id == "PROD-URGENT"
    assert reschedule.delay_days == 15  # ceil(20 - 5), a real, large number

    # THE GAP: nothing marks this plan as unacceptable. It returns a normal,
    # well-formed Plan object -- same shape as any other -- with no signal
    # anywhere that a HIGH-priority order just got pushed 3x past its
    # original deadline, and no reference to AGENTS.md rule 3's "no supplier
    # meets deadline" trigger anywhere in this module.
    assert plan is not None  # a plan WAS produced -- nothing refused it
    source = open("app/engine/planner.py", encoding="utf-8").read()
    assert "no supplier meets deadline" not in source.lower()
    # No escalation-flag field exists on Plan at all -- a caller inspecting
    # this object has nothing to check for "does this plan actually work."
    escalation_related_fields = [
        f for f in type(plan).model_fields if "escalat" in f.lower() or "deadline" in f.lower()
    ]
    assert escalation_related_fields == []

    # The rejected alternative's regret text is genuinely misleading by
    # omission: it explains why SUP-RISKY lost (reliability), but never
    # mentions that picking it would have hit the deadline the chosen
    # option missed by 15 days. An auditor reading only rejected_alternatives
    # would not learn that a viable, on-time option was passed over.
    risky_alt = next(
        a for a in plan.rejected_alternatives if a.option == "SUP-RISKY only"
    )
    assert "deadline" not in risky_alt.regret.lower()
    assert "on_time" not in risky_alt.regret.lower()
    assert "reliability_score" in risky_alt.regret  # what it DOES say instead


# ==========================================================================
# Item 5b — safety-stock draw, gated on written justification
# ==========================================================================


def _put_inventory(store, component_id, usable_stock, safety_stock, daily_usage):
    from app.environment.schemas import InventoryRecord

    store.inventory[component_id] = InventoryRecord(
        component_id=component_id, name="x", current_stock=usable_stock,
        usable_stock=usable_stock, daily_usage=daily_usage, safety_stock=safety_stock,
        warehouse="x", last_updated=NOW,
    )


def test_smoke_safety_stock_is_the_only_thing_standing_between_the_plan_and_the_deadline(
    store,
):
    """The scenario the task explicitly asks for: WITHOUT drawing safety
    stock, the order needs part of a slow (8-day) shipment and misses its
    5.5-day deadline. WITH the draw (the full 100-unit reserve), on-hand
    alone (200 operating + 100 drawn = 300) fully covers the order's 300-unit
    need immediately -- comfortably on time. Confirms the justification names
    the real gap (2.0 days), the real component, and the real order."""
    _put_inventory(store, "COMP-SAFETY", usable_stock=300, safety_stock=100, daily_usage=50)

    slow_combo = _combo("SLOW", price=1000, lead=8, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-SAFETY", quantity_needed=100,
        pareto_set=[slow_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    urgent = _coverage_result(
        "PROD-SAFETY", "high", days_to_deadline=5.5, component_required=300,
        component_id="COMP-SAFETY",
    )

    # Precondition: WITHOUT the draw, the reserve-respecting pool (200) plus
    # the slow 100-unit shipment (day 8) is genuinely late against 5.5 days.
    baseline = allocate_stock([urgent], on_hand=200, combination=slow_combo)
    assert baseline[0].on_time is False
    assert baseline[0].earliest_full_supply_day == 8.0

    plan = run_planner(store, "COMP-SAFETY", result, [urgent], now=NOW)

    decision = plan.safety_stock_decision
    assert decision.triggered is True
    assert decision.drawn is True
    assert decision.draw_units == 100  # capped at the full safety_stock
    assert decision.gap_days == 2.0  # earliest_secured_delivery(8) - days_of_coverage_on_hand(6.0)

    draw_actions = plan.safety_stock_actions()
    assert len(draw_actions) == 1
    draw = draw_actions[0]
    assert draw.component_id == "COMP-SAFETY"
    assert draw.units == 100
    assert draw.reversibility == "compensable"

    # The real payoff: the order that WOULD have missed its deadline is now
    # on time, and there is no reschedule action for it.
    detail = plan.allocations[0]
    assert detail.production_order_id == "PROD-SAFETY"
    assert detail.on_time is True
    assert detail.earliest_full_supply_day == 0.0  # covered entirely on-hand now
    assert plan.reschedule_actions() == []

    # Justification names the real numbers, not a template.
    assert "COMP-SAFETY" in draw.justification
    assert "2.0" in draw.justification
    assert "100" in draw.justification
    assert "PROD-SAFETY" in draw.justification


def test_draw_is_capped_at_the_spec_safety_stock_field_not_a_second_definition(store):
    """A gap bigger than safety_stock can cover: the draw stops at the
    reserve ceiling, it does not manufacture extra units."""
    _put_inventory(store, "COMP-CAP", usable_stock=300, safety_stock=40, daily_usage=50)
    slow_combo = _combo("SLOW", price=1000, lead=10, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-CAP", quantity_needed=100,
        pareto_set=[slow_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order = _coverage_result(
        "PROD-CAP", "high", days_to_deadline=9.0, component_required=300,
        component_id="COMP-CAP",
    )
    plan = run_planner(store, "COMP-CAP", result, [order], now=NOW)

    assert plan.safety_stock_decision.draw_units == 40  # capped, not the full gap's units
    assert plan.safety_stock_decision.draw_units <= 40  # == InventoryRecord.safety_stock


def test_no_trigger_when_reserve_respecting_allocation_already_makes_the_deadline(store):
    """The trigger must not fire reflexively just because a chosen
    combination exists -- only when a real gap remains."""
    _put_inventory(store, "COMP-FINE", usable_stock=300, safety_stock=100, daily_usage=50)
    fast_combo = _combo("FAST", price=1000, lead=1, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-FINE", quantity_needed=100,
        pareto_set=[fast_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order = _coverage_result(
        "PROD-FINE", "high", days_to_deadline=10.0, component_required=300,
        component_id="COMP-FINE",
    )
    plan = run_planner(store, "COMP-FINE", result, [order], now=NOW)

    assert plan.safety_stock_decision.triggered is False
    assert plan.safety_stock_decision.drawn is False
    assert plan.safety_stock_actions() == []


def test_draw_is_refused_when_it_would_worsen_another_order_sharing_the_component(store):
    """Two orders share one component. Order A's gap would need the reserve;
    order B has a very tight deadline that the untouched reserve currently
    protects. Drawing to help A would flip B's on-hand-only classification
    to a worse one -- the draw must be refused, not authorized with a
    footnote."""
    _put_inventory(store, "COMP-SHARED", usable_stock=200, safety_stock=150, daily_usage=50)
    slow_combo = _combo("SLOW", price=1000, lead=6, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-SHARED", quantity_needed=100,
        pareto_set=[slow_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order_a = _coverage_result(
        "PROD-A", "high", days_to_deadline=5.0, component_required=250,
        component_id="COMP-SHARED",
    )
    order_b = _coverage_result(
        "PROD-B", "low", days_to_deadline=0.9, component_required=10,
        component_id="COMP-SHARED",
    )

    plan = run_planner(store, "COMP-SHARED", result, [order_a, order_b], now=NOW)

    decision = plan.safety_stock_decision
    assert decision.triggered is True
    assert decision.drawn is False
    assert "PROD-B" in decision.worsened_orders
    assert plan.safety_stock_actions() == []
    assert "PROD-B" in decision.reason


def test_justification_is_computed_not_boilerplate(store):
    """Two different scenarios must produce two different justification
    strings, proving the numbers are computed, not a fixed template."""
    _put_inventory(store, "COMP-J1", usable_stock=300, safety_stock=100, daily_usage=50)
    _put_inventory(store, "COMP-J2", usable_stock=400, safety_stock=80, daily_usage=40)

    combo1 = _combo("S1", price=1000, lead=8, reliability=0.8, qty=100, moq=10, avail=100)
    combo2 = _combo("S2", price=1000, lead=15, reliability=0.8, qty=80, moq=10, avail=80)

    result1 = SolverResult(
        computed_at=NOW, component_id="COMP-J1", quantity_needed=100,
        pareto_set=[combo1], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    result2 = SolverResult(
        computed_at=NOW, component_id="COMP-J2", quantity_needed=80,
        pareto_set=[combo2], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order1 = _coverage_result(
        "PROD-J1", "high", days_to_deadline=5.5, component_required=300, component_id="COMP-J1"
    )
    order2 = _coverage_result(
        "PROD-J2", "high", days_to_deadline=9.0, component_required=400, component_id="COMP-J2"
    )

    plan1 = run_planner(store, "COMP-J1", result1, [order1], now=NOW)
    plan2 = run_planner(store, "COMP-J2", result2, [order2], now=NOW)

    j1 = plan1.safety_stock_actions()[0].justification
    j2 = plan2.safety_stock_actions()[0].justification

    assert j1 != j2
    assert "COMP-J1" in j1 and "COMP-J2" not in j1
    assert "COMP-J2" in j2 and "COMP-J1" not in j2
    assert "PROD-J1" in j1
    assert "PROD-J2" in j2


def test_safety_stock_draw_is_never_tagged_irreversible(store):
    _put_inventory(store, "COMP-REV", usable_stock=300, safety_stock=100, daily_usage=50)
    slow_combo = _combo("SLOW", price=1000, lead=8, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-REV", quantity_needed=100,
        pareto_set=[slow_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order = _coverage_result(
        "PROD-REV", "high", days_to_deadline=5.5, component_required=300,
        component_id="COMP-REV",
    )
    plan = run_planner(store, "COMP-REV", result, [order], now=NOW)

    draw = plan.safety_stock_actions()[0]
    assert draw.reversibility == "compensable"
    assert draw.reversibility != "irreversible"


def test_safety_stock_decision_is_always_present_even_when_nothing_triggers(store):
    """Mirrors the PollDecision/VerificationSkip pattern: the attempt is
    always recorded, not just positive outcomes."""
    combo = _combo("X", price=1000, lead=3, reliability=0.8, qty=100, moq=10, avail=100)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _coverage_result("PROD-A", "high", days_to_deadline=10, component_required=100)
    plan = run_planner(store, "COMP-X", result, [cov], now=NOW)

    assert plan.safety_stock_decision is not None
    assert isinstance(plan.safety_stock_decision.reason, str)
    assert len(plan.safety_stock_decision.reason) > 0


def test_allocate_stock_respects_the_reserve_by_default_via_run_planner(store):
    """Documents the real behavior change made while building 5b: routine
    allocation no longer silently spends into the reserve. Precondition
    check against a case where the difference is visible."""
    _put_inventory(store, "COMP-RESV", usable_stock=300, safety_stock=100, daily_usage=50)
    fast_combo = _combo("FAST", price=1000, lead=1, reliability=0.8, qty=50, moq=10, avail=50)
    result = SolverResult(
        computed_at=NOW, component_id="COMP-RESV", quantity_needed=50,
        pareto_set=[fast_combo], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    order = _coverage_result(
        "PROD-RESV", "high", days_to_deadline=10.0, component_required=250,
        component_id="COMP-RESV",
    )
    plan = run_planner(store, "COMP-RESV", result, [order], now=NOW)

    # 200 operating on-hand + 50 incoming = 250 -- exactly enough without
    # ever touching the reserve.
    detail = plan.allocations[0]
    assert detail.allocated_on_hand == 200
    assert detail.total_allocated == 250
    assert detail.shortfall == 0
    assert plan.safety_stock_decision.triggered is False


def test_on_hand_only_status_reuses_coverages_classify_not_a_reimplementation():
    """Structural: the reclassification logic must import coverage.py's
    _classify, not redefine the healthy/at_risk/critical threshold ladder."""
    source = open("app/engine/planner.py", encoding="utf-8").read()
    assert "_coverage_classify" in source
    assert "import" in source and "_classify as _coverage_classify" in source
