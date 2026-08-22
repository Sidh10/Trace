"""PROVENANCE GRAPH tests — ARCHITECTURE.md §4 item 7.

The load-bearing tests here are the wiring ones: each of the five sources
item 7 must record (items 2b, 3, 4, 5, 6) gets its own test asserting the
specific relation type and the specific object it cites. Plus the two
structural guarantees that make the trail trustworthy rather than
decorative: no invented score on any edge (AGENTS.md rule 7), and regret
figures held BY REFERENCE to planner's own objects so a discrepancy is
impossible rather than merely unlikely.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.audit.provenance import (
    NODE_KINDS,
    Assumption,
    DecisionRecord,
    ProvenanceEdge,
    build_provenance_graph,
    node,
    reset_provenance_sequences,
)
from app.engine.coverage import CoverageResult, compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import reset_plan_sequence, run_planner
from app.engine.ratchet import run_ratchet
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

RELATIONS = {"Support", "Depend-on", "Contradict", "Invalidate", "Trigger", "Update"}


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    return build_store()


def _full_pipeline(store, now=NOW):
    """The real Beat 2/3/4 sequence, end to end, against the seeded dataset.
    Returns every report the graph builder consumes."""
    store.send_supplier_message(
        supplier_id="SUP-21", to="supplier21@example.com", subject="x",
        body="Any update on PO-7712?",
    )
    coverage = compute_coverage(store, now=now)
    monitor = run_monitor_cycle(store, coverage=coverage, now=now)
    verification = run_verification_cycle(store, coverage=coverage, monitor=monitor, now=now)

    store.purchase_orders["PO-7712"].status = "delayed"
    coverage = compute_coverage(store, now=now)
    comp104 = [r for r in coverage.results if r.component_id == "COMP-104"]
    need = sum(r.component_required for r in comp104)

    solver_result = run_solver(store, component_id="COMP-104", quantity_needed=need, now=now)
    plan = run_planner(store, "COMP-104", solver_result, comp104, now=now)
    brief = run_ratchet(store, plan, solver_result, now=now)
    return coverage, monitor, verification, solver_result, plan, brief


def _graph(store, now=NOW):
    coverage, monitor, verification, solver_result, plan, brief = _full_pipeline(store, now)
    graph = build_provenance_graph(
        coverage=coverage, monitor=monitor, verification=verification,
        solver_result=solver_result, plan=plan, brief=brief, now=now,
    )
    return graph, (coverage, monitor, verification, solver_result, plan, brief)


def _cov(pid, priority, days_to_deadline, required, component_id="COMP-X"):
    return CoverageResult(
        production_order_id=pid, product="x", component_id=component_id, component_name="x",
        days_of_coverage_on_hand=0.0, days_of_coverage=0.0,
        deadline=NOW + timedelta(days=days_to_deadline), priority=priority,
        days_to_deadline=days_to_deadline, status="critical", is_thin=True,
        reason="stockout_before_deadline", safety_stock=0, days_to_safety_breach=math.inf,
        component_required=required, usable_stock=0, daily_usage=90,
    )


def _combo(sid, price, lead, reliability, qty=100, moq=10, avail=100):
    option = SupplierOption(
        supplier_id=sid, unit_price=price / qty, quantity=qty, available_quantity=avail,
        lead_time_days=lead, reliability_score=reliability, quality_score=0.9,
        min_order_quantity=moq,
    )
    return SourcingCombination(
        members=[option], quantity_needed=qty, quantity_allocated=qty, total_price=price,
        lead_time_days=lead, reliability_score=reliability, quality_score=0.9,
        total_min_order_quantity=moq, total_available_quantity=avail,
    )


# ==========================================================================
# Schema discipline — six relations, no invented scores
# ==========================================================================


def test_only_the_six_architecture_relations_are_used(store):
    graph, _ = _graph(store)
    assert {e.relation for e in graph.edges} <= RELATIONS


def test_no_severity_or_confidence_field_exists_on_an_edge():
    """AGENTS.md rule 7: an edge exists or it doesn't. A weighted edge would
    be an invented metric displayed as a finding."""
    fields = set(ProvenanceEdge.model_fields.keys())
    for banned in ("severity", "confidence", "weight", "score", "strength", "certainty"):
        assert banned not in fields


def test_no_invented_score_fields_anywhere_in_the_graph_schema():
    from app.audit.provenance import ProvenanceGraph, ToolCallSummary

    for model in (ProvenanceEdge, Assumption, DecisionRecord, ToolCallSummary, ProvenanceGraph):
        for field in model.model_fields:
            assert "confidence" not in field.lower()
            assert "severity" not in field.lower()


def test_every_edge_cites_module_inputs_and_model_version(store):
    graph, _ = _graph(store)
    assert graph.edges  # guard: an empty graph would make this vacuous
    for edge in graph.edges:
        assert edge.produced_by_module
        assert edge.input_record_ids, f"{edge.edge_id} cites no input records"
        assert edge.model_version
        assert edge.note.strip()


def test_every_node_kind_is_declared(store):
    graph, _ = _graph(store)
    assert graph.unknown_node_kinds() == []
    for n in graph.nodes():
        assert n.split(":", 1)[0] in NODE_KINDS


def test_architecture_from_to_aliases_are_preserved_in_serialization(store):
    """§7's shape names these `from`/`to`; `from` is a Python keyword, so the
    repo's established alias pattern (schemas.py SupplierMessage.from_) is
    used. The serialized JSON must still match the doc."""
    graph, _ = _graph(store)
    dumped = graph.edges[0].model_dump(by_alias=True)
    assert "from" in dumped and "to" in dumped


# ==========================================================================
# Wiring — item 2b (monitor)
# ==========================================================================


def test_monitor_poll_supports_the_disruption_event_it_detected(store):
    graph, (_, monitor, _, _, _, _) = _graph(store)

    event = next(e for e in monitor.events if e.po_id == "PO-7712")
    edges = [
        e for e in graph.edges
        if e.relation == "Support"
        and e.from_node == node("poll", "PO-7712")
        and e.to_node == node("event", event.event_id)
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.produced_by_module == "monitor"
    assert "PO-7712" in edge.input_record_ids
    assert "label_created_no_pickup" in edge.note


def test_monitor_poll_records_why_it_was_worth_spending(store):
    """The load-bearing justification must survive into the trail — it is
    the pre-disruption snapshot a later coverage pass no longer shows."""
    graph, _ = _graph(store)
    edge = next(
        e for e in graph.edges
        if e.from_node == node("poll", "PO-7712") and e.relation == "Support"
    )
    assert "PROD-882" in edge.input_record_ids
    assert "PROD-882" in edge.note


def test_a_poll_that_found_nothing_gets_an_explicit_update_edge(store):
    """Silence must never be ambiguous between 'checked, consistent' and
    'never checked' — the same principle item 6's no-trigger edge uses."""
    graph, (_, monitor, _, _, _, _) = _graph(store)

    clean_polls = [d for d in monitor.polled() if not d.contradicts_po_status]
    assert clean_polls  # guard
    for decision in clean_polls:
        edges = [
            e for e in graph.edges
            if e.relation == "Update" and e.from_node == node("poll", decision.po_id)
        ]
        assert len(edges) == 1
        assert "consistent" in edges[0].note


# ==========================================================================
# Wiring — item 3 (verify)
# ==========================================================================


def test_verify_contradiction_is_a_contradict_edge_tracking_to_claim(store):
    graph, (_, _, verification, _, _, _) = _graph(store)

    check = verification.for_po("PO-7712")
    assert check.contradicted is True  # guard

    edges = [
        e for e in graph.edges
        if e.relation == "Contradict"
        and e.from_node == node("tracking", "PO-7712")
        and e.to_node == node("claim", check.claim.message_id)
    ]
    assert len(edges) == 1
    assert edges[0].note == check.contradiction_reason  # verbatim


def test_verify_contradiction_invalidates_the_reliability_score(store):
    graph, (_, _, verification, _, _, _) = _graph(store)
    check = verification.for_po("PO-7712")

    edges = [
        e for e in graph.edges
        if e.relation == "Invalidate" and e.to_node == node("reliability", "SUP-21")
    ]
    assert len(edges) == 1
    edge = edges[0]
    # AGENTS.md rule 4 made structural: the score moved because TRACKING
    # contradicted the claim. The edge's own origin says so.
    assert edge.from_node == node("tracking", "PO-7712")
    assert str(check.reliability_before) in edge.note
    assert str(check.reliability_after) in edge.note
    assert "wording" in edge.note  # explicitly disclaims tone as the cause


def test_verify_edges_carry_item_3s_real_model_version_field(store):
    graph, (_, _, verification, _, _, _) = _graph(store)
    check = verification.for_po("PO-7712")

    verify_edges = [e for e in graph.edges if e.produced_by_module == "verify"]
    assert verify_edges
    for edge in verify_edges:
        assert edge.model_version == check.claim.model_version


def test_verify_model_version_follows_the_llm_path_when_it_succeeds(store, monkeypatch):
    """The whole point of reusing item 3's field: when the LLM genuinely
    parsed the claim, the edge says so — with the real pinned model string,
    not an invented one."""
    from app.llm import gemini_client

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    monkeypatch.setattr(
        "app.llm.gemini_client.parse_supplier_claim",
        lambda _b: gemini_client.LLMParsedClaim(
            po_id="PO-7712", claim_status="dispatched", claimed_delay_days=0
        ),
    )
    # Both LLM entry points must be mocked, not just the one under test —
    # _full_pipeline reaches the ratchet's narration too, and an unmocked
    # call there is a live network request inside the suite.
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm] {text}"
    )

    graph, (_, _, verification, _, _, _) = _graph(store)
    check = verification.for_po("PO-7712")
    assert check.claim.parsed_by == "llm"
    assert check.claim.model_version == gemini_client.MODEL_VERSION

    verify_edges = [e for e in graph.edges if e.produced_by_module == "verify"]
    assert all(e.model_version == gemini_client.MODEL_VERSION for e in verify_edges)
    assert all(e.model_version != "deterministic" for e in verify_edges)


# ==========================================================================
# Wiring — item 4 (solver)
# ==========================================================================


def test_solver_rejection_gets_a_support_edge_citing_the_exact_drop_reason(store):
    graph, (_, _, _, solver_result, _, _) = _graph(store)

    rejection = next(r for r in solver_result.rejected if r.subject == "SUP-18")
    assert rejection.reason == "quality_below_threshold"  # guard

    edges = [
        e for e in graph.edges
        if e.relation == "Support" and e.to_node == node("rejection", "SUP-18")
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.produced_by_module == "solver"
    assert "quality_below_threshold" in edge.note
    assert rejection.note in edge.note  # verbatim, not restated


def test_every_solver_rejection_reason_reaches_the_graph(store):
    graph, (_, _, _, solver_result, _, _) = _graph(store)
    for rejection in solver_result.rejected:
        assert any(
            e.to_node == node("rejection", rejection.subject)
            and rejection.reason in e.note
            for e in graph.edges
        )


# ==========================================================================
# Wiring — item 5 (planner deadline filter)
# ==========================================================================


def test_deadline_infeasible_combination_gets_an_invalidate_edge_naming_the_order(store):
    """Synthetic, because the real dataset is deadline-feasible: a
    combination the filter dropped must be Invalidated, citing which
    order's deadline it would have missed."""
    reset_provenance_sequences()
    slow = _combo("SLOW", price=1000, lead=20, reliability=0.95)
    fast = _combo("FAST", price=900, lead=2, reliability=0.5)
    solver_result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[slow, fast], rejected=[], quotes_requested=2, quotes_reused=0,
    )
    urgent = _cov("PROD-URGENT", "high", days_to_deadline=5.0, required=100)
    plan = run_planner(store, "COMP-X", solver_result, [urgent], now=NOW)

    graph = build_provenance_graph(solver_result=solver_result, plan=plan, now=NOW)

    edges = [
        e for e in graph.edges
        if e.relation == "Invalidate" and e.to_node == node("combination", "SLOW only")
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.produced_by_module == "planner"
    assert edge.from_node == node("deadline_constraint", "COMP-X")
    # The order and the day figures come through verbatim from the
    # RejectedAlternative's own regret — not re-parsed or restated.
    assert "PROD-URGENT" in edge.note
    rejected = next(
        a for a in plan.rejected_alternatives if a.option == "SLOW only"
    )
    assert edge.note == rejected.regret


def test_a_feasible_but_outranked_combination_is_not_invalidated(store):
    """Only the deadline filter produces Invalidate. A candidate that merely
    lost SELECTION_RULE's tiers is not 'invalid' — conflating the two would
    misrepresent why it wasn't chosen."""
    reset_provenance_sequences()
    winner = _combo("A", price=1000, lead=3, reliability=0.9)
    loser = _combo("B", price=900, lead=3, reliability=0.5)
    solver_result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[winner, loser], rejected=[], quotes_requested=2, quotes_reused=0,
    )
    cov = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan = run_planner(store, "COMP-X", solver_result, [cov], now=NOW)
    graph = build_provenance_graph(solver_result=solver_result, plan=plan, now=NOW)

    assert not [
        e for e in graph.edges
        if e.relation == "Invalidate" and e.to_node == node("combination", "B only")
    ]


# ==========================================================================
# Wiring — item 6 (ratchet)
# ==========================================================================


def test_each_fired_trigger_gets_a_trigger_edge_into_the_brief(store):
    reset_provenance_sequences()
    expensive = _combo("EXPENSIVE", price=200_000, lead=1, reliability=0.9)
    solver_result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[expensive], rejected=[], quotes_requested=1, quotes_reused=0,
    )
    cov = _cov("PROD-A", "high", days_to_deadline=10.0, required=100)
    plan = run_planner(store, "COMP-X", solver_result, [cov], now=NOW)
    brief = run_ratchet(store, plan, solver_result, now=NOW)
    assert brief.triggers_fired == ["cost_above_threshold"]  # guard

    graph = build_provenance_graph(solver_result=solver_result, plan=plan, brief=brief, now=NOW)
    edges = [e for e in graph.edges if e.relation == "Trigger" and e.produced_by_module == "ratchet"]
    assert len(edges) == 1
    assert edges[0].from_node == node("trigger", "cost_above_threshold")
    assert edges[0].to_node == node("brief", plan.plan_id)
    assert "Non-overridable" in edges[0].note


def test_no_trigger_fired_gets_an_explicit_update_edge_not_silence(store):
    graph, (_, _, _, _, plan, brief) = _graph(store)
    assert brief.triggers_fired == []  # the real dataset executes cleanly

    edges = [
        e for e in graph.edges
        if e.relation == "Update" and e.produced_by_module == "ratchet"
    ]
    assert len(edges) == 1
    edge = edges[0]
    assert edge.from_node == node("ratchet_check", "COMP-104")
    assert edge.to_node == node("brief", plan.plan_id)
    # Names all three so "none fired" is provably "all three were checked".
    assert "cost_above_threshold" in edge.note
    assert "no_feasible_deadline_plan" in edge.note
    assert "quality_risk" in edge.note


def test_ratchet_edges_are_deterministic_even_when_narration_used_the_llm(store, monkeypatch):
    """The decision comes from _evaluate_triggers (pure Python); only the
    narration may be LLM-authored. Tagging a Trigger edge with the
    narration's model version would assert in the audit trail that an LLM
    decided the escalation — the exact claim AGENTS.md rules 1 and 3 make
    false."""
    from app.llm import gemini_client

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm] {text}"
    )
    # Mock the parse path too — otherwise _full_pipeline's verify stage
    # makes a live network call inside the suite.
    monkeypatch.setattr(
        "app.llm.gemini_client.parse_supplier_claim",
        lambda _b: gemini_client.LLMParsedClaim(
            po_id="PO-7712", claim_status="dispatched", claimed_delay_days=0
        ),
    )

    graph, (_, _, _, _, plan, brief) = _graph(store)
    assert brief.narrated_by == "llm"  # guard: the LLM really did run
    assert brief.model_version != "deterministic"

    ratchet_edges = [e for e in graph.edges if e.produced_by_module == "ratchet"]
    assert ratchet_edges
    for edge in ratchet_edges:
        assert edge.model_version == "deterministic"

    # ...but the narration's provenance is not lost — it is recorded in the
    # field that actually describes narration.
    record = next(d for d in graph.decisions if d.module == "ratchet")
    assert record.model_version == "deterministic"
    assert record.narration_model_version == brief.model_version


# ==========================================================================
# Regret — the same objects, not copies
# ==========================================================================


def test_regret_ledger_holds_planners_own_objects_by_reference(store):
    """Cite the same object so a discrepancy is structurally impossible.
    Pydantic preserves instance identity for already-validated models, so
    this is object identity, not value equality."""
    graph, (_, _, _, _, plan, _) = _graph(store)

    assert len(graph.regret_ledger) == len(plan.rejected_alternatives)
    for recorded, original in zip(graph.regret_ledger, plan.rejected_alternatives):
        assert recorded is original


def test_cost_of_inaction_is_planners_own_object_by_reference(store):
    graph, (_, _, _, _, plan, _) = _graph(store)
    assert graph.cost_of_inaction is plan.cost_of_inaction


def test_graph_never_writes_a_second_copy_of_a_regret_number():
    """Structural: provenance.py must not read `saved` /
    `cost_increase_vs_baseline_pct` / `units_unbuilt` and restate them. It
    holds the objects; it does not transcribe their figures."""
    source = open("app/audit/provenance.py", encoding="utf-8").read()
    assert ".saved" not in source
    assert "cost_increase_vs_baseline_pct" not in source.split('"""')[-1]
    assert "units_unbuilt" not in source.split('"""')[-1]


# ==========================================================================
# Assumption ledger — merged into the same object, not a second one
# ==========================================================================


def test_graph_is_one_object_carrying_trail_and_ledger_together(store):
    graph, _ = _graph(store)
    assert graph.edges          # the audit trail
    assert graph.assumptions    # the assumption ledger
    assert graph.decisions      # the reproducibility log
    assert graph.tool_calls     # tool-call accounting


def test_assumptions_quote_their_source_field_verbatim(store):
    graph, (_, _, _, _, plan, brief) = _graph(store)

    by_field = {a.source_field: a for a in graph.assumptions}
    assert by_field["Plan.selection_rule"].statement == plan.selection_rule
    assert (
        by_field["Plan.cost_of_inaction.baseline_note"].statement
        == plan.cost_of_inaction.baseline_note
    )
    assert (
        by_field["DecisionBrief.falsification_line"].statement == brief.falsification_line
    )


def test_the_falsification_line_is_in_the_ledger(store):
    """'What would have to be true for this to be wrong' is the assumption
    ledger's whole reason to exist — it must be recorded, not just narrated."""
    graph, (_, _, _, _, _, brief) = _graph(store)
    assert any(a.statement == brief.falsification_line for a in graph.assumptions)


# ==========================================================================
# Per-decision reproducibility log (AGENTS.md rule 7)
# ==========================================================================


def test_every_decision_carries_a_model_version(store):
    graph, _ = _graph(store)
    assert graph.decisions
    for record in graph.decisions:
        assert record.model_version
        assert record.module
        assert record.input_record_ids


def test_a_decision_is_logged_for_each_deciding_module(store):
    graph, _ = _graph(store)
    modules = {d.module for d in graph.decisions}
    assert {"verify", "solver", "planner", "ratchet"} <= modules


# ==========================================================================
# Tool-call count vs necessity (item 10 consumes this)
# ==========================================================================


def test_tool_call_summary_totals_only_upstream_counters(store):
    graph, (_, monitor, verification, solver_result, _, brief) = _graph(store)
    summary = graph.tool_calls

    assert summary.monitor_polls_made == monitor.polls_made
    assert summary.monitor_polls_available == monitor.polls_available
    assert summary.verify_probes_made == verification.probes_made
    assert summary.verify_probes_reused_from_monitor == verification.probes_reused_from_monitor
    assert summary.solver_quotes_requested == solver_result.quotes_requested
    assert summary.ratchet_approval_checks_made == brief.approval_checks_made

    assert summary.total_calls_made == (
        monitor.polls_made
        + verification.probes_made
        + solver_result.quotes_requested
        + brief.approval_checks_made
    )


def test_calls_avoided_by_gating_is_counted_not_scored(store):
    graph, (_, monitor, verification, solver_result, _, _) = _graph(store)
    expected = (
        (monitor.polls_available - monitor.polls_made)
        + verification.probes_reused_from_monitor
        + solver_result.quotes_reused
    )
    assert graph.tool_calls.calls_avoided_by_gating == expected
    assert graph.tool_calls.notes  # a plain explanation per module, no ratio


def test_no_efficiency_ratio_is_invented_here():
    """Item 10 owns whatever ratio it wants; inventing one now would be a
    metric this module has no mandate for (AGENTS.md rule 7)."""
    from app.audit.provenance import ToolCallSummary

    for field in ToolCallSummary.model_fields:
        assert "ratio" not in field.lower()
        assert "efficiency" not in field.lower()
        assert "pct" not in field.lower()


# ==========================================================================
# Integrity — the end-to-end trace, no dangling or duplicated edges
# ==========================================================================


def test_smoke_graph_traces_from_monitors_poll_to_the_decision_brief(store):
    """The requirement item 7 exists to satisfy: an unbroken, directed path
    from the moment MONITOR spent a tracking call to the decision that came
    out the far end."""
    graph, (_, _, _, _, plan, _) = _graph(store)

    path = graph.trace_path(node("poll", "PO-7712"), node("brief", plan.plan_id))
    assert path is not None, "no directed path from the poll to the brief"

    relations = [e.relation for e in path]
    assert relations == ["Support", "Trigger", "Support"]
    assert path[0].from_node == node("poll", "PO-7712")
    assert path[-1].to_node == node("brief", plan.plan_id)


def test_smoke_graph_has_no_duplicated_edges(store):
    graph, _ = _graph(store)
    assert graph.duplicate_edges() == []


def test_smoke_graph_has_no_dangling_node_kinds(store):
    graph, _ = _graph(store)
    assert graph.unknown_node_kinds() == []


def test_trace_path_returns_none_for_genuinely_disconnected_nodes(store):
    """The trace check has to be capable of failing, or the test above
    proves nothing."""
    graph, _ = _graph(store)
    assert graph.trace_path(node("brief", "PLAN-0001"), node("poll", "PO-7712")) is None


# ==========================================================================
# Determinism and LLM independence
# ==========================================================================


def test_graph_construction_imports_no_llm():
    source = open("app/audit/provenance.py", encoding="utf-8").read()
    assert "from google" not in source
    assert "import google" not in source
    assert "gemini_client" not in source
    assert "narrate" not in source


def test_repeated_builds_over_the_same_reports_agree(store):
    _, reports = _graph(store)
    coverage, monitor, verification, solver_result, plan, brief = reports

    reset_provenance_sequences()
    first = build_provenance_graph(
        coverage=coverage, monitor=monitor, verification=verification,
        solver_result=solver_result, plan=plan, brief=brief, now=NOW,
    )
    reset_provenance_sequences()
    second = build_provenance_graph(
        coverage=coverage, monitor=monitor, verification=verification,
        solver_result=solver_result, plan=plan, brief=brief, now=NOW,
    )
    assert [e.model_dump(by_alias=True) for e in first.edges] == [
        e.model_dump(by_alias=True) for e in second.edges
    ]


def test_graph_builds_identically_in_both_llm_modes_except_verify_model_version(
    store, monkeypatch
):
    """AGENTS.md rule 2. The graph's STRUCTURE must not depend on the flag;
    only the verify edges' model_version legitimately differs, because that
    is the field whose entire job is to record which parser ran."""
    from app.llm import gemini_client as gemini_client_module

    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    graph_off, _ = _graph(store)

    clock.reset()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    store_on = build_store()
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", True)
    monkeypatch.setattr(
        "app.llm.gemini_client.narrate_decision", lambda text: f"[llm] {text}"
    )
    monkeypatch.setattr(
        "app.llm.gemini_client.parse_supplier_claim",
        lambda _b: gemini_client_module.LLMParsedClaim(
            po_id="PO-7712", claim_status="dispatched", claimed_delay_days=0
        ),
    )
    graph_on, _ = _graph(store_on)

    shape_off = [(e.relation, e.from_node, e.to_node) for e in graph_off.edges]
    shape_on = [(e.relation, e.from_node, e.to_node) for e in graph_on.edges]
    assert shape_off == shape_on


# ==========================================================================
# Partial graphs
# ==========================================================================


def test_a_partial_pipeline_still_produces_a_valid_graph(store):
    """Not every run reaches the ratchet. A graph built from whatever ran
    must still be internally valid rather than requiring a full pass."""
    coverage = compute_coverage(store, now=NOW)
    monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)

    graph = build_provenance_graph(coverage=coverage, monitor=monitor, now=NOW)
    assert graph.edges
    assert graph.unknown_node_kinds() == []
    assert graph.duplicate_edges() == []
    assert graph.regret_ledger == []
    assert graph.cost_of_inaction is None


def test_an_empty_graph_is_valid_not_a_crash():
    reset_provenance_sequences()
    graph = build_provenance_graph(now=NOW)
    assert graph.edges == []
    assert graph.assumptions == []
    assert graph.tool_calls.total_calls_made == 0


def test_a_rejection_with_no_plan_still_records_solver_evidence(store):
    """Quality wiped out the candidate pool: no plan exists, but WHY there
    is no plan is exactly what the audit trail must still show."""
    reset_provenance_sequences()
    rejection = Rejection(
        subject="SUP-BAD", reason="quality_below_threshold",
        note="quality too low", estimated_unit_price=10.0,
    )
    solver_result = SolverResult(
        computed_at=NOW, component_id="COMP-X", quantity_needed=100,
        pareto_set=[], rejected=[rejection], quotes_requested=0, quotes_reused=0,
    )
    cov = _cov("PROD-A", "high", days_to_deadline=5.0, required=100)
    plan = run_planner(store, "COMP-X", solver_result, [cov], now=NOW)
    assert plan is None
    brief = run_ratchet(store, plan, solver_result, now=NOW)

    graph = build_provenance_graph(solver_result=solver_result, plan=plan, brief=brief, now=NOW)
    assert any(
        e.to_node == node("rejection", "SUP-BAD") and "quality_below_threshold" in e.note
        for e in graph.edges
    )
    # And the escalation that followed is recorded too.
    assert any(e.relation == "Trigger" for e in graph.edges)


# ==========================================================================
# Read-only over the environment
# ==========================================================================


def test_building_the_graph_does_not_mutate_the_store(store):
    _, reports = _graph(store)
    coverage, monitor, verification, solver_result, plan, brief = reports

    before = {
        "suppliers": [s.model_dump(mode="json") for s in store.list_suppliers()],
        "pos": [p.model_dump(mode="json") for p in store.list_purchase_orders()],
        "erp_log": len(store.erp_log),
        "rfq_log": len(store.rfq_log),
    }
    build_provenance_graph(
        coverage=coverage, monitor=monitor, verification=verification,
        solver_result=solver_result, plan=plan, brief=brief, now=NOW,
    )
    assert [s.model_dump(mode="json") for s in store.list_suppliers()] == before["suppliers"]
    assert [p.model_dump(mode="json") for p in store.list_purchase_orders()] == before["pos"]
    assert len(store.erp_log) == before["erp_log"]
    assert len(store.rfq_log) == before["rfq_log"]
