"""CONTINGENCY tests — ARCHITECTURE.md §4 item 9.

This closes the "expedited delivery becomes unavailable" hidden test
(ARCHITECTURE.md §10), which had nothing covering it before.

The property that distinguishes a contingency from "just replan again" is
the one most worth guarding: when a pre-committed trigger fires, re-entry
lands at PLAN and **spends no RFQ call**, because the fallback was already
on the Pareto set at plan time. A version that quietly re-solved would pass
every other test in this file, so `test_..._spends_no_rfq_call` and
`test_..._reenters_at_plan_not_solver` are the load-bearing ones.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.routes import reset_orchestrator_state, resolve_approval, run_pipeline
from app.audit.provenance import node, reset_provenance_sequences
from app.engine.contingency import (
    CONTINGENCY_REENTRY_STAGE,
    TRIGGER_EVENT_TYPE,
    ContingencyPlan,
    FailureTrigger,
    evaluate_contingencies,
    register_contingencies,
    to_staleness_report,
)
from app.engine.coverage import reset_event_sequence
from app.engine.planner import reset_plan_sequence
from app.environment import seed_data
from app.environment.clock import clock
from app.environment.schemas import RFQQuote
from app.main import app

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)

_ELICIT_CLAIM = {
    "to": "supplier21@example.com",
    "subject": "Status check on PO-7712",
    "body": "Any update on PO-7712?",
}


@pytest.fixture
def store():
    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()
    seed_data.STATE.send_supplier_message(supplier_id="SUP-21", **_ELICIT_CLAIM)
    return seed_data.STATE


@pytest.fixture
def escalating(monkeypatch):
    """Escalate so the plan exists but is NOT written — a contingency must be
    able to swap a primary that has not yet become irreversible."""
    monkeypatch.setattr(config, "TRACE_APPROVAL_THRESHOLD", 60_000.0)


def _plan(store):
    return run_pipeline(store, component_id="COMP-104", now=NOW)


def _withdraw_expedite(store, supplier_id: str) -> None:
    """The disruption: a NEW quote for this supplier no longer offers
    expedited delivery. `expedite_available` is §5.7's own field — this is
    the literal hidden-test condition, injected the way ARCHITECTURE.md §5
    permits ("mid-run disruption injection")."""
    prior = next(
        q for q in reversed(store.rfq_log)
        if q.supplier_id == supplier_id and q.component_id == "COMP-104"
    )
    store.rfq_log.append(
        RFQQuote(
            supplier_id=supplier_id,
            component_id="COMP-104",
            quantity_available=prior.quantity_available,
            unit_price=prior.unit_price,
            delivery_days=prior.delivery_days,
            expedite_available=False,  # withdrawn
            expedite_fee=0.0,
            quote_valid_hours=prior.quote_valid_hours,
            quote_issued_at=clock.now(),
        )
    )


# ==========================================================================
# Registration — pre-committed AT PLAN TIME
# ==========================================================================


def test_contingencies_are_registered_when_the_plan_is_built(store, escalating):
    run = _plan(store)

    assert run.contingencies, "no fallback was pre-committed"
    splits = {a.supplier_id for a in run.brief.chosen_plan.purchase_actions()}
    assert {c.primary_action.supplier_id for c in run.contingencies} == splits


def test_each_contingency_has_the_three_required_parts(store, escalating):
    """ARCHITECTURE.md §4 item 9's own shape: {primary_action,
    failure_trigger: {condition}, fallback_action}."""
    run = _plan(store)

    for contingency in run.contingencies:
        assert contingency.primary_action is not None
        assert contingency.failure_trigger.condition.strip()
        assert contingency.fallback_action is not None
        # The fallback must genuinely differ from the primary, or it is not
        # a fallback.
        assert (
            contingency.fallback_action.supplier_id
            != contingency.primary_action.supplier_id
        )


def test_the_fallback_comes_from_the_pareto_set_not_somewhere_new(store, escalating):
    """A contingency pre-commits to an option SOLVER already found. Inventing
    an alternative would be a second, unranked solver."""
    run = _plan(store)
    not_selected = {
        alt.option
        for alt in run.graph.regret_ledger
        if alt.reason == "not_selected"
    }
    for contingency in run.contingencies:
        label = contingency.fallback_combination.label
        # It is the chosen plan's sibling on the Pareto front, surfaced by
        # PLANNER as a not-selected alternative — never a fresh construction.
        assert label in not_selected


def test_no_contingency_is_registered_when_there_is_no_alternative(store, escalating):
    """A contingency with no fallback is not a contingency. Rather than
    manufacture one, none is registered."""
    from app.engine.contingency import register_contingencies as register
    from app.engine.solver import SolverResult

    run = _plan(store)
    plan = run.brief.chosen_plan
    solo = SolverResult(
        computed_at=NOW, component_id="COMP-104", quantity_needed=950,
        pareto_set=[plan.chosen_combination], rejected=[],
        quotes_requested=0, quotes_reused=0, quotes_used={},
    )
    assert register(plan, solo, now=NOW) == []


def test_reversibility_tags_are_reused_from_planner_not_re_derived(store, escalating):
    """Rule 5 tags come from `planner.py`'s own actions. A second source of
    truth for reversibility is exactly how the two drift apart."""
    run = _plan(store)
    for contingency in run.contingencies:
        assert contingency.primary_reversibility == contingency.primary_action.reversibility
        assert contingency.fallback_reversibility == contingency.fallback_action.reversibility
        assert contingency.primary_reversibility == "compensable"


# ==========================================================================
# THE HIDDEN TEST — expedited delivery becomes unavailable
# ==========================================================================


def test_expedite_withdrawn_fires_the_pre_committed_fallback(store, escalating):
    """ARCHITECTURE.md §10's 'Expedited delivery unavailable' row, which had
    nothing covering it before item 9."""
    first = _plan(store)
    primary = first.contingencies[0].primary_action.supplier_id
    _withdraw_expedite(store, primary)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.fired_contingencies, "expedite withdrawal did not fire a contingency"
    fired = second.fired_contingencies[0]
    assert fired.contingency.failure_trigger.kind == "expedite_withdrawn"
    assert fired.contingency.failure_trigger.subject == primary
    assert second.plan_id != first.plan_id
    assert second.superseded_plan_id == first.plan_id


def test_the_fallback_supplier_actually_replaces_the_primary(store, escalating):
    first = _plan(store)
    contingency = first.contingencies[0]
    primary = contingency.primary_action.supplier_id
    _withdraw_expedite(store, primary)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    new_suppliers = {a.supplier_id for a in second.brief.chosen_plan.purchase_actions()}
    assert primary not in new_suppliers, "the withdrawn supplier is still in the plan"
    assert contingency.fallback_action.supplier_id in new_suppliers


def test_expedite_trigger_maps_to_the_declared_disruption_event_type():
    """ARCHITECTURE.md §7's enum already declared `expedite_unavailable`;
    nothing emitted it until item 9."""
    assert TRIGGER_EVENT_TYPE["expedite_withdrawn"] == "expedite_unavailable"
    assert TRIGGER_EVENT_TYPE["lead_time_exceeded"] == "expedite_unavailable"


def test_lead_time_exceeded_is_the_same_failure_through_the_catalog(store, escalating):
    """The operational form: whatever the reason, the delivery the plan was
    built on is no longer achievable in the time it assumed."""
    first = _plan(store)
    contingency = next(
        c for c in first.contingencies if c.failure_trigger.kind == "expedite_withdrawn"
    )
    # Force the catalog-side trigger by re-registering against a plan whose
    # quote had no expedite promise, then raising the lead time.
    trigger = FailureTrigger(
        kind="lead_time_exceeded",
        subject=contingency.primary_action.supplier_id,
        condition="lead time rises above what the plan assumed",
        observed_field="SupplierRecord.lead_time_days",
        plan_assumed=str(contingency.primary_action.lead_time_days),
    )
    catalog_contingency = contingency.model_copy(update={"failure_trigger": trigger})

    assert evaluate_contingencies(store, [catalog_contingency], now=NOW) == []

    store.suppliers[contingency.primary_action.supplier_id].lead_time_days += 5
    fired = evaluate_contingencies(store, [catalog_contingency], now=NOW)
    assert len(fired) == 1
    assert fired[0].contingency.failure_trigger.kind == "lead_time_exceeded"


# ==========================================================================
# Built ON item 8, not beside it — the load-bearing properties
# ==========================================================================


def test_a_fired_contingency_reenters_at_plan_not_solver(store, escalating):
    """The whole point. Re-entry lands at PLAN because the fallback is
    already chosen; SOLVER is skipped."""
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == "PLAN"
    assert CONTINGENCY_REENTRY_STAGE == "PLAN"
    joined = " | ".join(second.stages)
    assert "HARD FILTER + SOLVER (reused" in joined, "SOLVER re-ran — the fallback was re-solved"
    assert "COVERAGE (reused" in joined
    assert "MONITOR (reused" in joined


def test_a_fired_contingency_spends_no_rfq_call(store, escalating):
    """The measurable saving that justifies pre-committing at all. A version
    that quietly re-solved would pass every other test here."""
    first = _plan(store)
    rfq_before = len(store.rfq_log)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    rfq_after_injection = len(store.rfq_log)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert len(store.rfq_log) == rfq_after_injection, "the replan issued a fresh RFQ"
    assert second.graph.tool_calls.solver_quotes_requested == 0
    assert rfq_after_injection == rfq_before + 1  # only the injected quote


def test_it_reuses_item_8s_report_shape_not_a_parallel_one(store, escalating):
    """One re-entry mechanism, not two: a fired contingency is rendered as
    item 8's own StalenessReport so the orchestrator consumes it unchanged."""
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    report = second.staleness
    assert report is not None
    assert report.reentry_stage == "PLAN"
    assert report.stages_to_rerun == ["PLAN", "RATCHET"]
    assert all(f.kind.startswith("contingency_fired:") for f in report.findings)


def test_the_fallback_still_passes_planners_own_checks(store, escalating):
    """A contingency changes WHICH combination is planned, never HOW. The
    fallback goes through the same deadline filter and allocation, so it can
    still be found infeasible — it is not waved through for being
    pre-committed."""
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    plan = second.brief.chosen_plan
    assert plan.selection_rule  # went through run_planner unchanged
    assert plan.allocations
    assert plan.cost_of_inaction is not None
    assert isinstance(plan.deadline_feasible, bool)


def test_the_fallback_is_verified_not_trusted_for_being_pre_committed(store, escalating):
    """Post-replan verification applies to a fallback too. Being chosen
    earlier says nothing about whether it still holds now."""
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.post_replan_verified is True
    assert any("POST-REPLAN VERIFICATION" in s for s in second.stages)


def test_contingency_is_checked_before_generic_staleness(store, escalating):
    """A lead-time rise is BOTH generic SOLVER-level staleness and the
    contingency's own trigger. The pre-commitment must win — otherwise
    registering it was pointless and an RFQ call is spent rediscovering an
    answer already recorded."""
    first = _plan(store)
    primary = first.contingencies[0].primary_action.supplier_id
    _withdraw_expedite(store, primary)
    # Also move a supplier field, which alone would be SOLVER-level staleness.
    store.suppliers[primary].lead_time_days += 3

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.fired_contingencies, "generic staleness pre-empted the contingency"
    assert second.reentered_at_stage == "PLAN"  # not SOLVER
    assert second.graph.tool_calls.solver_quotes_requested == 0


# ==========================================================================
# Provenance — the Trigger edge citing the condition BY NAME
# ==========================================================================


def test_provenance_shows_a_trigger_edge_naming_the_failure_condition(store, escalating):
    first = _plan(store)
    contingency = first.contingencies[0]
    _withdraw_expedite(store, contingency.primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    fired_id = second.fired_contingencies[0].contingency.contingency_id
    edges = [
        e for e in second.graph.edges
        if e.relation == "Trigger" and e.from_node == node("contingency", fired_id)
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.to_node == node("plan", second.plan_id)
    # Named, not merely referenced by id.
    assert "expedite_withdrawn" in edge.note
    assert "expedite_available" in edge.note
    assert fired_id in edge.input_record_ids


def test_provenance_records_what_the_fallback_replaced(store, escalating):
    first = _plan(store)
    contingency = first.contingencies[0]
    primary = contingency.primary_action.supplier_id
    _withdraw_expedite(store, primary)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    fired_id = second.fired_contingencies[0].contingency.contingency_id
    invalidations = [
        e for e in second.graph.edges
        if e.relation == "Invalidate" and e.from_node == node("contingency", fired_id)
    ]
    assert len(invalidations) == 1
    assert invalidations[0].to_node == node("supplier", primary)
    assert "compensable" in invalidations[0].note


def test_the_contingency_graph_stays_structurally_clean(store, escalating):
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.graph.unknown_node_kinds() == []
    assert second.graph.duplicate_edges() == []


# ==========================================================================
# The ERP boundary is unchanged
# ==========================================================================


def test_a_fired_contingency_on_an_escalated_plan_writes_nothing(store, escalating):
    first = _plan(store)
    assert len(store.erp_log) == 0
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.decision == "escalate"
    assert second.erp_writes == []
    assert len(store.erp_log) == 0


def test_a_fired_contingency_after_execution_supersedes_rather_than_substitutes(store):
    """Once the primary has been written it is irreversible — there is
    nothing left to swap. The replan supersedes; the POs already created
    stand (§5 excludes cancellation)."""
    first = _plan(store)
    assert first.decision == "execute"
    writes_before = len(store.erp_log)
    assert writes_before > 0

    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.superseded_plan_id == first.plan_id
    assert any("SUPERSEDES rather than substitutes" in s for s in second.stages)
    # The earlier irreversible writes are untouched.
    assert len(store.erp_log) >= writes_before


def test_approving_the_fallback_plan_writes_its_own_pos(store, escalating):
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    resolve_approval(store, plan_id=second.plan_id, approved=True)

    written = {
        w.resulting_state.get("plan_id")
        for w in store.erp_log
        if w.action == "store_plan"
    }
    assert written == {second.plan_id}


# ==========================================================================
# Through the real HTTP orchestrator
# ==========================================================================


def test_http_expedite_unavailable_end_to_end(store, escalating):
    client = TestClient(app)

    first = client.post("/agent/handle-event", json={"po_id": "PO-7712"}).json()
    assert first["contingencies"], "no contingency was pre-committed"
    primary = first["contingencies"][0]["primary_action"]["supplier_id"]

    _withdraw_expedite(store, primary)

    second = client.post("/agent/handle-event", json={"po_id": "PO-7712"}).json()

    assert second["fired_contingencies"]
    assert second["reentered_at_stage"] == "PLAN"
    assert second["plan_id"] != first["plan_id"]
    assert second["graph"]["tool_calls"]["solver_quotes_requested"] == 0
    assert second["erp_writes"] == []

    trigger_notes = [
        e["note"] for e in second["graph"]["edges"]
        if e["relation"] == "Trigger" and e["from"].startswith("contingency:")
    ]
    assert trigger_notes
    assert "expedite_withdrawn" in trigger_notes[0]


# ==========================================================================
# Constraints
# ==========================================================================


def test_no_llm_in_the_contingency_path():
    source = open("app/engine/contingency.py", encoding="utf-8").read()
    assert "gemini" not in source.lower()
    assert "from google" not in source


def test_no_invented_thresholds_in_the_trigger_conditions():
    """Every comparison targets the plan's OWN recorded assumption, never a
    constant someone chose. Checked against executable code, not prose."""
    from conftest import executable_source

    code = executable_source("app/engine/contingency.py")
    # The plan's own assumption is what each condition compares against.
    assert "plan_assumed" in code
    for banned in ("THRESHOLD", "_LIMIT", "TOLERANCE", "MARGIN"):
        assert banned not in code


def test_evaluating_triggers_spends_no_tool_calls(store, escalating):
    """Triggers are free reads off records the pipeline already holds."""
    run = _plan(store)
    rfq_before = len(store.rfq_log)
    erp_before = len(store.erp_log)

    evaluate_contingencies(store, run.contingencies, now=NOW)

    assert len(store.rfq_log) == rfq_before
    assert len(store.erp_log) == erp_before


def test_both_llm_modes_fire_the_same_contingency(store, escalating, monkeypatch):
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    first = _plan(store)
    _withdraw_expedite(store, first.contingencies[0].primary_action.supplier_id)
    off = run_pipeline(store, component_id="COMP-104", now=NOW)

    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()
    on_store = seed_data.STATE
    on_store.send_supplier_message(supplier_id="SUP-21", **_ELICIT_CLAIM)

    from app.llm import gemini_client

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm] {text}"
    )
    monkeypatch.setattr(
        "app.llm.gemini_client.parse_supplier_claim",
        lambda _b: gemini_client.LLMParsedClaim(
            po_id="PO-7712", claim_status="dispatched", claimed_delay_days=0
        ),
    )
    on_first = run_pipeline(on_store, component_id="COMP-104", now=NOW)
    _withdraw_expedite(on_store, on_first.contingencies[0].primary_action.supplier_id)
    on = run_pipeline(on_store, component_id="COMP-104", now=NOW)

    assert off.reentered_at_stage == on.reentered_at_stage
    assert [f.contingency.failure_trigger.kind for f in off.fired_contingencies] == [
        f.contingency.failure_trigger.kind for f in on.fired_contingencies
    ]


def test_to_staleness_report_returns_none_when_nothing_fired():
    assert to_staleness_report([], now=NOW) is None
