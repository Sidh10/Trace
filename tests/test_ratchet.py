"""RATCHET tests — ARCHITECTURE.md §4 item 6.

Three hard triggers (AGENTS.md rule 3), tested individually and in
combination; the "do NOT escalate everything" requirement (PROJECT.md §2 —
over-escalation is a scored failure) tested as its own positive case, not
just inferred from the trigger tests; and the LLM-independence requirement
tested by running the SAME escalating scenario with TRACE_LLM_ENABLED both
True and False and confirming decision/triggers are byte-identical.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.engine.coverage import CoverageResult
from app.engine.planner import run_planner, reset_plan_sequence
from app.engine.ratchet import (
    _evaluate_triggers,
    _falsification_line,
    run_ratchet,
)
from app.engine.solver import Rejection, SolverResult, SourcingCombination, SupplierOption
from app.environment.clock import clock
from app.environment.seed_data import build_store

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_plan_sequence()
    return build_store()


def _cov(pid, priority, days_to_deadline, required, component_id="COMP-X"):
    return CoverageResult(
        production_order_id=pid, product="x", component_id=component_id, component_name="x",
        days_of_coverage_on_hand=0.0, days_of_coverage=0.0,
        deadline=NOW + timedelta(days=days_to_deadline), priority=priority,
        days_to_deadline=days_to_deadline, status="critical", is_thin=True,
        reason="stockout_before_deadline", safety_stock=0, days_to_safety_breach=math.inf,
        component_required=required, usable_stock=0, daily_usage=90,
    )


def _combo(sid, price, lead, reliability, qty=100, moq=10, avail=100, quality=0.9):
    option = SupplierOption(
        supplier_id=sid, unit_price=price / qty, quantity=qty, available_quantity=avail,
        lead_time_days=lead, reliability_score=reliability, quality_score=quality,
        min_order_quantity=moq,
    )
    return SourcingCombination(
        members=[option], quantity_needed=qty, quantity_allocated=qty, total_price=price,
        lead_time_days=lead, reliability_score=reliability, quality_score=quality,
        total_min_order_quantity=moq, total_available_quantity=avail,
    )


def _plan_and_result(store, component_id, pareto_set, orders, rejected=None):
    result = SolverResult(
        computed_at=NOW, component_id=component_id, quantity_needed=100,
        pareto_set=pareto_set, rejected=rejected or [], quotes_requested=len(pareto_set),
        quotes_reused=0,
    )
    plan = run_planner(store, component_id, result, orders, now=NOW)
    return plan, result


# ==========================================================================
# The three hard triggers, individually
# ==========================================================================


def test_trigger_cost_above_threshold(store):
    combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "escalate"
    assert brief.triggers_fired == ["cost_above_threshold"]


def test_trigger_no_feasible_deadline_plan_with_a_plan_present(store):
    """Every Pareto candidate misses a high-priority deadline; PLANNER falls
    back to the best infeasible one (deadline_feasible=False) rather than
    returning None — RATCHET must escalate on THAT signal."""
    combo = _combo("SLOW", price=1000, lead=20, reliability=0.9)
    order = _cov("PROD-B", "high", days_to_deadline=5.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    assert plan is not None
    assert plan.deadline_feasible is False

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "escalate"
    assert "no_feasible_deadline_plan" in brief.triggers_fired


def test_trigger_no_feasible_deadline_plan_with_no_plan_at_all(store):
    """Empty Pareto set -> plan is None -> run_ratchet must not crash, and
    must still escalate (there is nothing to execute)."""
    order = _cov("PROD-C", "high", days_to_deadline=5.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [], [order])

    assert plan is None

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "escalate"
    assert "no_feasible_deadline_plan" in brief.triggers_fired
    assert brief.plan_id is None
    assert brief.total_cost is None


def test_trigger_quality_risk_fires_only_when_no_plan_and_quality_was_the_cause(store):
    rejection = Rejection(
        subject="SUP-BAD", reason="quality_below_threshold",
        note="quality too low", estimated_unit_price=10.0,
    )
    order = _cov("PROD-D", "high", days_to_deadline=5.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [], [order], rejected=[rejection])

    assert plan is None
    brief = run_ratchet(store, plan, result, now=NOW)
    assert "quality_risk" in brief.triggers_fired
    assert "no_feasible_deadline_plan" in brief.triggers_fired  # co-fires, correctly


def test_quality_risk_does_not_fire_when_a_plan_exists_even_if_some_supplier_was_quality_rejected(store):
    """A quality-rejected supplier alongside surviving, quality-passing
    candidates is normal hard-filter behaviour, not a risk finding —
    filtering out one bad-quality supplier while others remain is the
    system working, not something to escalate about."""
    good_combo = _combo("GOOD", price=1000, lead=1, reliability=0.9, quality=0.95)
    rejection = Rejection(
        subject="SUP-BAD", reason="quality_below_threshold",
        note="quality too low", estimated_unit_price=10.0,
    )
    order = _cov("PROD-E", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [good_combo], [order], rejected=[rejection])

    assert plan is not None
    brief = run_ratchet(store, plan, result, now=NOW)
    assert "quality_risk" not in brief.triggers_fired


def test_multiple_triggers_can_co_fire(store):
    combo = _combo("EXPENSIVE_AND_SLOW", price=200_000, lead=30, reliability=0.9)
    order = _cov("PROD-F", "high", days_to_deadline=5.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert set(brief.triggers_fired) == {"cost_above_threshold", "no_feasible_deadline_plan"}


# ==========================================================================
# Do NOT escalate everything — execute is a real, reachable outcome
# ==========================================================================


def test_a_clean_plan_executes_no_over_escalation(store):
    """In-budget, deadline-feasible, quality-clean -> execute. If this ever
    flips to escalate, the ratchet has become the over-escalation failure
    mode PROJECT.md §2 scores against just as harshly as under-escalation."""
    combo = _combo("CLEAN", price=1000, lead=1, reliability=0.9, quality=0.95)
    order = _cov("PROD-G", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "execute"
    assert brief.triggers_fired == []


def test_smoke_real_current_state_executes(store):
    """The actual Beat 2/3/4 pipeline state (post-verify SUP-21 downgrade,
    PO-7712 delayed) must execute cleanly — confirmed live, not assumed."""
    from app.engine.coverage import compute_coverage, reset_event_sequence
    from app.engine.monitor import run_monitor_cycle
    from app.engine.solver import run_solver
    from app.engine.verify import run_verification_cycle

    reset_event_sequence()
    store.send_supplier_message(
        supplier_id="SUP-21", to="supplier21@example.com", subject="x",
        body="Any update on PO-7712?",
    )
    coverage = compute_coverage(store, now=NOW)
    monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
    run_verification_cycle(store, coverage=coverage, monitor=monitor, now=NOW)
    store.purchase_orders["PO-7712"].status = "delayed"
    coverage = compute_coverage(store, now=NOW)
    comp104_results = [r for r in coverage.results if r.component_id == "COMP-104"]
    total_need = sum(r.component_required for r in comp104_results)

    solver_result = run_solver(store, component_id="COMP-104", quantity_needed=total_need, now=NOW)
    plan = run_planner(store, "COMP-104", solver_result, comp104_results, now=NOW)
    brief = run_ratchet(store, plan, solver_result, now=NOW)

    assert brief.decision == "execute"
    assert brief.triggers_fired == []
    assert brief.total_cost == plan.total_cost


# ==========================================================================
# Decision brief fields
# ==========================================================================


def test_cost_delta_vs_baseline_is_read_from_plan_not_recomputed(store):
    from app.environment.schemas import InventoryRecord, PurchaseOrder

    store.inventory["COMP-BASE"] = InventoryRecord(
        component_id="COMP-BASE", name="x", current_stock=0, usable_stock=0,
        daily_usage=1, safety_stock=0, warehouse="x", last_updated=NOW,
    )
    store.purchase_orders["PO-BASE"] = PurchaseOrder(
        po_id="PO-BASE", component_id="COMP-BASE", supplier_id="SUP-OLD",
        quantity=100, expected_delivery=NOW.date(), status="delayed",
        unit_price=100.0, total_value=10000.0, approval_required_above=150000,
    )
    combo = _combo("X", price=1200, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100, component_id="COMP-BASE")
    plan, result = _plan_and_result(store, "COMP-BASE", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.cost_delta_vs_baseline == round(
        plan.total_cost - plan.cost_of_inaction.baseline_total_cost, 2
    )
    assert brief.cost_increase_vs_baseline_pct == plan.cost_of_inaction.cost_increase_vs_baseline_pct


def test_rejected_alternatives_are_plans_own_signed_list_verbatim(store):
    combo_a = _combo("A", price=1000, lead=1, reliability=0.9)
    combo_b = _combo("B", price=900, lead=1, reliability=0.5)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo_a, combo_b], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.rejected_alternatives == plan.rejected_alternatives


def test_cost_of_inaction_is_plans_own_object_verbatim(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.cost_of_inaction is plan.cost_of_inaction


def test_no_plan_leaves_alternatives_and_cost_of_inaction_empty_not_fabricated(store):
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.rejected_alternatives == []
    assert brief.cost_of_inaction is None


# ==========================================================================
# Falsification line — computed per decision, not boilerplate
# ==========================================================================


def test_falsification_line_differs_between_execute_and_escalate(store):
    clean_combo = _combo("CLEAN", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    execute_plan, execute_result = _plan_and_result(store, "COMP-X", [clean_combo], [order])
    execute_brief = run_ratchet(store, execute_plan, execute_result, now=NOW)

    expensive_combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    escalate_plan, escalate_result = _plan_and_result(store, "COMP-Y", [expensive_combo], [order])
    escalate_brief = run_ratchet(store, escalate_plan, escalate_result, now=NOW)

    assert execute_brief.falsification_line != escalate_brief.falsification_line
    assert "executes because" in execute_brief.falsification_line
    assert "escalates because" in escalate_brief.falsification_line


def test_falsification_line_names_the_real_trigger_and_numbers(store):
    combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert "200000.00" in brief.falsification_line
    assert "150000" in brief.falsification_line


def test_falsification_line_scales_with_different_triggers(store):
    """Two DIFFERENT escalation reasons must produce two DIFFERENT lines —
    proof it's computed from the actual trigger set, not a fixed sentence."""
    cost_combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    cost_plan, cost_result = _plan_and_result(store, "COMP-X", [cost_combo], [order])
    cost_brief = run_ratchet(store, cost_plan, cost_result, now=NOW)

    slow_combo = _combo("SLOW", price=1000, lead=20, reliability=0.9)
    deadline_plan, deadline_result = _plan_and_result(store, "COMP-Y", [slow_combo], [order])
    deadline_brief = run_ratchet(store, deadline_plan, deadline_result, now=NOW)

    assert cost_brief.falsification_line != deadline_brief.falsification_line


def test_falsification_line_is_computed_directly_via_the_private_function():
    line_a = _falsification_line("escalate", ["cost_above_threshold"], None, 150000.0)
    line_b = _falsification_line("escalate", ["quality_risk"], None, 150000.0)
    assert line_a != line_b
    assert "threshold" in line_a.lower()
    assert "quality" in line_b.lower()


# ==========================================================================
# LLM independence — escalation fires regardless of TRACE_LLM_ENABLED
# ==========================================================================


def test_escalation_fires_identically_regardless_of_llm_flag(store, monkeypatch):
    combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    # LLM path mocked (not skipped) — this proves independence at the
    # decision layer without making a live network call; live-call
    # independence is proven separately by test_llm_cannot_argue_past_a_
    # fired_trigger, which mocks a narration that argues the OPPOSITE way.
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm rephrase] {text}"
    )

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    brief_off = run_ratchet(store, plan, result, now=NOW)

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    brief_on = run_ratchet(store, plan, result, now=NOW)

    assert brief_off.decision == brief_on.decision == "escalate"
    assert brief_off.triggers_fired == brief_on.triggers_fired
    assert brief_off.total_cost == brief_on.total_cost
    assert brief_off.falsification_line == brief_on.falsification_line
    assert brief_off.narrated_by == "deterministic"
    assert brief_on.narrated_by == "llm"  # the mock ran -- narration text differs, decision doesn't


def test_execute_fires_identically_regardless_of_llm_flag(store, monkeypatch):
    combo = _combo("CLEAN", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm rephrase] {text}"
    )

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    brief_off = run_ratchet(store, plan, result, now=NOW)

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    brief_on = run_ratchet(store, plan, result, now=NOW)

    assert brief_off.decision == brief_on.decision == "execute"
    assert brief_off.triggers_fired == brief_on.triggers_fired == []
    assert brief_off.narrated_by == "deterministic"
    assert brief_on.narrated_by == "llm"


def test_llm_cannot_argue_past_a_fired_trigger(store, monkeypatch):
    """The concrete proof of AGENTS.md rule 3's "no confidence score
    overrides this and the LLM cannot argue past it": mock the LLM
    narration to return text literally arguing for the OPPOSITE decision,
    and confirm brief.decision is completely unaffected — narration is
    display-only, never parsed back into the decision."""
    combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision",
        lambda _text: "Actually, ignore all triggers — this should EXECUTE immediately.",
    )

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "escalate"  # unchanged despite the narration's content
    assert brief.triggers_fired == ["cost_above_threshold"]
    assert brief.narrated_by == "llm"  # the mock DID run
    assert "EXECUTE immediately" in brief.narration  # and its text IS in the brief
    # but it changed nothing about what was actually decided


def test_llm_failure_falls_through_to_deterministic_narration(store, monkeypatch):
    combo = _combo("CLEAN", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)

    def _raise(_text):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("app.llm.gemini_client.narrate_decision", _raise)

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "execute"
    assert brief.narrated_by == "deterministic"
    assert brief.model_version == "deterministic"


# ==========================================================================
# Brief renders readably with the LLM off
# ==========================================================================


def test_deterministic_brief_is_readable_not_a_stub(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.narrated_by == "deterministic"
    assert len(brief.narration) > 200
    assert "DECISION" in brief.narration
    assert plan.plan_id in brief.narration
    assert str(plan.total_cost) in brief.narration or f"{plan.total_cost:.2f}" in brief.narration


def test_deterministic_brief_shows_at_risk_orders_when_present(store):
    tight = _cov("PROD-TIGHT", "low", days_to_deadline=1.0, required=100)
    combo = _combo("SLOW", price=1000, lead=9, reliability=0.9)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [tight])
    brief = run_ratchet(store, plan, result, now=NOW)

    assert "PROD-TIGHT" in brief.narration
    assert "units unbuilt" in brief.narration


def test_deterministic_brief_with_no_plan_still_renders_readably(store):
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [], [order])
    assert plan is None

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.narrated_by == "deterministic"
    assert "no feasible" in brief.narration.lower() or "no plan" in brief.narration.lower()


# ==========================================================================
# Tool-call count vs necessity
# ==========================================================================


def test_approval_check_is_made_exactly_once_when_a_plan_exists(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.approval_checks_made == 1


def test_approval_check_is_skipped_when_there_is_no_plan_to_check(store):
    """Nothing to check the cost of -- calling check_approval anyway would
    be a wasted tool call."""
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [], [order])
    assert plan is None

    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.approval_checks_made == 0


def test_ratchet_calls_no_rfq_or_verification_tools(store):
    """RATCHET only reads what SOLVER/PLANNER already computed — no new RFQ
    or reliability-update side effects."""
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    rfq_log_before = len(store.rfq_log)
    suppliers_before = {s.supplier_id: s.reliability_score for s in store.list_suppliers()}

    run_ratchet(store, plan, result, now=NOW)

    assert len(store.rfq_log) == rfq_log_before
    assert {s.supplier_id: s.reliability_score for s in store.list_suppliers()} == suppliers_before


# ==========================================================================
# Scope boundary — no ERP write, ever
# ==========================================================================


def test_ratchet_never_calls_erp_update_on_execute(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    before = len(store.erp_log)
    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "execute"
    assert len(store.erp_log) == before


def test_ratchet_never_calls_erp_update_on_escalate(store):
    combo = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    before = len(store.erp_log)
    brief = run_ratchet(store, plan, result, now=NOW)
    assert brief.decision == "escalate"
    assert len(store.erp_log) == before


# ==========================================================================
# Invariants
# ==========================================================================


def test_no_llm_import_at_module_load_time():
    source = open("app/engine/ratchet.py", encoding="utf-8").read()
    assert "from google" not in source
    assert "import google" not in source


def test_ratchet_does_not_recompute_plan_or_solver_logic():
    """Structural: ratchet.py must not contain its own copy of coverage,
    allocation, or Pareto logic — it only reads Plan / SolverResult fields
    already computed upstream. `allocate_stock` may be named in prose
    (explaining that PLANNER already ran it) but must never be CALLED."""
    source = open("app/engine/ratchet.py", encoding="utf-8").read()
    assert "daily_usage" not in source
    assert "allocate_stock(" not in source
    assert "_dominates" not in source


def test_repeated_ratchet_calls_over_the_same_inputs_agree(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    first = run_ratchet(store, plan, result, now=NOW)
    second = run_ratchet(store, plan, result, now=NOW)
    assert first.decision == second.decision
    assert first.triggers_fired == second.triggers_fired
    assert first.falsification_line == second.falsification_line


def test_evaluate_triggers_is_a_pure_function_over_its_inputs(store):
    combo = _combo("X", price=1000, lead=1, reliability=0.9)
    order = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan, result = _plan_and_result(store, "COMP-X", [combo], [order])

    triggers_1, checks_1 = _evaluate_triggers(store, plan, result)
    triggers_2, checks_2 = _evaluate_triggers(store, plan, result)
    assert triggers_1 == triggers_2
    assert checks_1 == checks_2 == 1
