"""ORCHESTRATOR tests — the assembled item-1-to-7 pipeline and, above all,
the ERP-write boundary (AGENTS.md rule 5).

The boundary tests are the point of this file. Everything else here supports
them. `POST /erp/update` is the single irreversible action in this system,
and these assert the three properties that make that claim true rather than
aspirational: it fires only on `execute`, it does not fire on `escalate`
until a human approves, and it cannot fire twice for one plan no matter how
the pipeline or the approval endpoint is called.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.routes import (
    reset_orchestrator_state,
    resolve_approval,
    run_pipeline,
)
from app.audit.provenance import build_provenance_graph, node, reset_provenance_sequences
from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import reset_plan_sequence, run_planner
from app.engine.ratchet import run_ratchet
from app.engine.solver import run_solver
from app.engine.verify import run_verification_cycle
from app.environment import seed_data
from app.environment.clock import clock
from app.main import app

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)

# The real PO-7712 disruption is the SEEDED state: tracking reads
# `label_created_no_pickup` while the PO still reads `in_transit`. That
# mismatch IS the disruption, and it is what MONITOR's load-bearing poll
# catches "before the supplier admits it" (PROJECT.md §4 Beat 2).
#
# Do NOT pre-mark PO-7712 `delayed` to "set up" the scenario: a delayed PO is
# no longer dependable inbound, so it stops being load-bearing, MONITOR never
# polls it, and the entire detection chain silently does not run. That was a
# real error in an earlier smoke test, caught by comparing edge counts.
_ELICIT_CLAIM = {
    "to": "supplier21@example.com",
    "subject": "Status check on PO-7712",
    "body": "Any update on PO-7712?",
}

# One EXECUTE writes a SET, not a single row: `store_plan` (the decision
# record) plus one `create_alternate_po` per purchase_split. The COMP-104
# plan splits across SUP-37 and SUP-42, so 1 + 2 = 3.
#
# The invariant these tests defend is NOT "exactly one row in the ERP log" —
# it is "exactly one write SET per plan_id, however the write is reached".
# Asserting a literal 1 would now fail for the right behaviour AND pass for a
# wrong one (a plan whose splits silently vanished).
_EXPECTED_SPLITS = 2
_EXPECTED_WRITES_PER_EXECUTED_PLAN = 1 + _EXPECTED_SPLITS


@pytest.fixture
def store():
    """A clean environment AND a clean orchestrator. Both are required:
    `build_store()` resets the simulator (ARCHITECTURE.md §8) but the
    orchestrator's run cache and ERP-write registry live outside it."""
    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()
    return seed_data.STATE


@pytest.fixture
def client(store):
    return TestClient(app)


def _elicit_claim(store):
    """SUP-21's scripted 'dispatched' reply — PROJECT.md §4 Beat 3."""
    return store.send_supplier_message(supplier_id="SUP-21", **_ELICIT_CLAIM)


def _force_escalate(monkeypatch, threshold: float = 60_000.0):
    """Drop the approval threshold so the real COMP-104 plan (~123,674)
    trips `cost_above_threshold`.

    60,000 specifically: it is above every COMP-104 supplier's minimum order
    commitment (SUP-21 59,000 / SUP-42 39,600 / SUP-37 25,200) so the solver's
    budget hard-filter still passes them all and a real plan still gets built,
    but below the plan's total so RATCHET escalates. A lower threshold (1,000)
    instead wipes out the candidate pool entirely and yields `plan=None` —
    also a valid escalation, covered separately, but useless for testing
    approval because there is nothing to approve.

    Patched on `app.config` rather than on `ratchet`: `Store.check_approval`
    imports the name locally at call time, which is what actually decides the
    trigger.
    """
    monkeypatch.setattr(config, "TRACE_APPROVAL_THRESHOLD", threshold)


# ==========================================================================
# THE ERP-WRITE BOUNDARY — the primary deliverable
# ==========================================================================


def test_erp_write_fires_when_the_verdict_is_execute(store):
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert run.decision == "execute"
    assert len(run.erp_writes) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert run.erp_writes[0].action == "store_plan"  # decision record first
    assert [w.action for w in run.erp_writes[1:]] == (
        ["create_alternate_po"] * _EXPECTED_SPLITS
    )
    assert run.awaiting_approval is False


def test_escalate_produces_zero_erp_writes_before_approval(store, monkeypatch):
    """Explicitly required: an escalate verdict must not write ANYTHING —
    not even `record_escalation`, which is itself one of §5.9's six actions
    and therefore itself an irreversible write."""
    _force_escalate(monkeypatch)
    _elicit_claim(store)

    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert run.decision == "escalate"
    assert run.brief.triggers_fired == ["cost_above_threshold"]
    assert run.erp_writes == []
    assert len(store.erp_log) == 0, "an escalate verdict wrote to the ERP"
    assert run.awaiting_approval is True
    # The plan still exists — it is withheld, not discarded.
    assert run.plan_id is not None


def test_escalate_with_no_feasible_plan_also_writes_nothing(store, monkeypatch):
    """The other escalate shape: the candidate pool is wiped out entirely, so
    `plan is None`. Must still write nothing and must not crash."""
    _force_escalate(monkeypatch, threshold=1_000.0)
    _elicit_claim(store)

    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert run.decision == "escalate"
    assert run.plan_id is None
    assert run.erp_writes == []
    assert len(store.erp_log) == 0


def test_firing_the_same_disruption_twice_produces_exactly_one_erp_write(store):
    """Explicitly required. Both idempotency guards have to hold for this:
    the run cache (so the second call doesn't mint a second plan_id) and the
    per-plan_id write guard."""
    _elicit_claim(store)

    first = run_pipeline(store, component_id="COMP-104", now=NOW)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert first.plan_id == second.plan_id
    assert first.run_id == second.run_id
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN, (
        f"expected one write SET ({_EXPECTED_WRITES_PER_EXECUTED_PLAN} rows), "
        f"got {len(store.erp_log)} — the second run wrote again"
    )
    assert [w.update_id for w in first.erp_writes] == [
        w.update_id for w in second.erp_writes
    ]
    # Exactly one PO per split — not two POs per split.
    created = [w for w in store.erp_log if w.action == "create_alternate_po"]
    assert len(created) == _EXPECTED_SPLITS
    assert len({w.resulting_state["po_id"] for w in created}) == _EXPECTED_SPLITS


def test_approving_twice_produces_exactly_one_erp_write(store, monkeypatch):
    _force_escalate(monkeypatch)
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == 0

    first = resolve_approval(store, plan_id=run.plan_id, approved=True)
    second = resolve_approval(store, plan_id=run.plan_id, approved=True)

    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert [w.update_id for w in first.erp_writes] == [
        w.update_id for w in second.erp_writes
    ]


def test_approval_after_an_execute_does_not_write_a_second_time(store):
    """The two paths into the write must share one guard, not two."""
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN

    resolve_approval(store, plan_id=run.plan_id, approved=True)
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN


def test_rejection_closes_the_plan_with_no_write_and_is_logged(store, monkeypatch):
    _force_escalate(monkeypatch)
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    resolved = resolve_approval(
        store, plan_id=run.plan_id, approved=False, approved_by="ops-lead",
        note="Buying at this price needs finance sign-off first.",
    )

    assert resolved.approval_outcome == "rejected"
    assert resolved.erp_writes == []
    assert len(store.erp_log) == 0, "a rejected plan wrote to the ERP"
    assert resolved.awaiting_approval is False
    assert "finance sign-off" in resolved.approval_note


def test_a_rejected_plan_can_still_not_be_written_by_re_running(store, monkeypatch):
    """Rejection must not be undone by simply calling the pipeline again."""
    _force_escalate(monkeypatch)
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    resolve_approval(store, plan_id=run.plan_id, approved=False)

    run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == 0


def test_the_orchestrator_is_the_only_caller_of_erp_update():
    """AGENTS.md rule 5 structurally: no engine module may call the one
    irreversible action. Only the orchestrator's guarded path does."""
    import pathlib

    for path in pathlib.Path("app/engine").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "update_erp(" not in source, f"{path} calls update_erp directly"
    assert "update_erp(" not in pathlib.Path("app/audit/provenance.py").read_text(
        encoding="utf-8"
    )


def test_every_erp_write_call_site_is_inside_the_one_guarded_function():
    """`_write_erp_once` now emits several actions (store_plan + one
    create_alternate_po per split), so counting call sites is no longer the
    invariant. What must hold is that every one of them sits INSIDE the
    single guarded function — nothing else in the orchestrator may write."""
    import ast

    tree = ast.parse(open("app/api/routes.py", encoding="utf-8").read())

    guarded, unguarded = 0, []
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        calls = [
            n
            for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "update_erp"
        ]
        if not calls:
            continue
        if func.name == "_write_erp_once":
            guarded += len(calls)
        else:
            unguarded.append(func.name)

    assert guarded >= 1
    assert unguarded == [], f"unguarded ERP writes in: {unguarded}"


# ==========================================================================
# Full chain, in the order §3 gives it
# ==========================================================================


def test_pipeline_walks_every_stage_in_order(store):
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    joined = " | ".join(run.stages)
    for stage in ("COVERAGE", "MONITOR", "VERIFY", "SOLVER", "PLAN", "RATCHET", "AUDIT"):
        assert stage in joined
    # COVERAGE precedes MONITOR — the data dependency, documented in the
    # module docstring, not a reordering of the logic.
    assert run.stages.index("COVERAGE") < next(
        i for i, s in enumerate(run.stages) if s.startswith("MONITOR")
    )
    assert run.stages[-1] == "AUDIT"


def test_pipeline_returns_both_the_brief_and_the_graph(store):
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert run.brief is not None
    assert run.graph is not None
    assert run.graph.edges
    assert run.graph.unknown_node_kinds() == []
    assert run.graph.duplicate_edges() == []


def test_the_graph_traces_from_monitors_poll_to_the_brief(store):
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    path = run.graph.trace_path(node("poll", "PO-7712"), node("brief", run.plan_id))
    assert path is not None
    assert [e.relation for e in path] == ["Support", "Trigger", "Support"]


def test_detection_chain_actually_runs_on_the_seeded_disruption(store):
    """Guard against the setup error described at the top of this file: if
    PO-7712 stops being load-bearing, MONITOR never polls it and the
    contradiction is never found — while the pipeline still returns a
    perfectly well-formed run. This asserts the chain really fired."""
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert any(
        e.from_node == node("poll", "PO-7712") and e.relation == "Support"
        for e in run.graph.edges
    ), "MONITOR never polled PO-7712 — the detection chain did not run"
    assert any(
        e.relation == "Contradict" and e.from_node == node("tracking", "PO-7712")
        for e in run.graph.edges
    ), "VERIFY never found the tracking contradiction"
    assert any(
        e.relation == "Invalidate" and e.to_node == node("reliability", "SUP-21")
        for e in run.graph.edges
    ), "SUP-21's reliability was never invalidated"


# ==========================================================================
# Through the real HTTP endpoint
# ==========================================================================


def test_http_handle_event_walks_the_full_pipeline(client, store):
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)

    response = client.post("/agent/handle-event", json={"po_id": "PO-7712"})
    assert response.status_code == 200
    body = response.json()

    assert body["component_id"] == "COMP-104"
    assert body["decision"] == "execute"
    assert len(body["erp_writes"]) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert body["graph"]["edges"]
    assert body["brief"]["falsification_line"]


def test_http_graph_is_identical_to_the_direct_call_graph(client, store):
    """Same inputs through HTTP and through direct calls must produce the
    same provenance graph, edge for edge — the orchestrator adds sequencing,
    not content."""
    # A: through HTTP.
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    http_body = client.post("/agent/handle-event", json={"po_id": "PO-7712"}).json()
    http_edges = [(e["relation"], e["from"], e["to"]) for e in http_body["graph"]["edges"]]

    # B: the same chain, called directly, on a fresh identical environment.
    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()
    direct_store = seed_data.STATE
    _elicit_claim(direct_store)

    coverage = compute_coverage(direct_store)
    monitor = run_monitor_cycle(direct_store, coverage=coverage)
    verification = run_verification_cycle(direct_store, coverage=coverage, monitor=monitor)
    results = [r for r in coverage.results if r.component_id == "COMP-104"]
    needed = sum(r.component_required for r in results)
    solver_result = run_solver(direct_store, component_id="COMP-104", quantity_needed=needed)
    plan = run_planner(direct_store, "COMP-104", solver_result, results)
    brief = run_ratchet(direct_store, plan, solver_result)
    graph = build_provenance_graph(
        coverage=coverage, monitor=monitor, verification=verification,
        solver_result=solver_result, plan=plan, brief=brief,
    )
    direct_edges = [(e.relation, e.from_node, e.to_node) for e in graph.edges]

    # The direct chain above performs no ERP write — it is engine calls only,
    # and the write is the orchestrator's own responsibility. So the HTTP
    # graph legitimately carries edges the direct one cannot: one per row the
    # orchestrator wrote. The honest assertion is therefore not "the two are
    # equal" but "the engine-derived edges are identical, and the ONLY thing
    # the orchestrator added is the ERP write trail" — which is a stronger
    # claim than equality, because it pins down exactly what the extra edges
    # are rather than just tolerating a difference.
    erp_node_prefixes = ("po:PO-9", "erp_write:")
    http_engine_edges = [
        e for e in http_edges if not e[2].startswith(erp_node_prefixes)
    ]
    http_erp_edges = [e for e in http_edges if e[2].startswith(erp_node_prefixes)]

    assert http_engine_edges == direct_edges
    assert len(http_erp_edges) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert http_body["decision"] == brief.decision
    assert http_body["plan_id"] == plan.plan_id


def test_http_escalate_then_approve_writes_exactly_once(client, store, monkeypatch):
    _force_escalate(monkeypatch)
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)

    body = client.post("/agent/handle-event", json={"component_id": "COMP-104"}).json()
    assert body["decision"] == "escalate"
    assert body["erp_writes"] == []
    assert body["awaiting_approval"] is True
    assert len(store.erp_log) == 0

    plan_id = body["plan_id"]
    first = client.post(f"/agent/approval/{plan_id}", json={"approved": True})
    assert first.status_code == 200
    assert len(first.json()["erp_writes"]) == _EXPECTED_WRITES_PER_EXECUTED_PLAN

    second = client.post(f"/agent/approval/{plan_id}", json={"approved": True})
    assert second.status_code == 200
    assert len(second.json()["erp_writes"]) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN


def test_http_rejection_writes_nothing(client, store, monkeypatch):
    _force_escalate(monkeypatch)
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    body = client.post("/agent/handle-event", json={"component_id": "COMP-104"}).json()

    response = client.post(
        f"/agent/approval/{body['plan_id']}",
        json={"approved": False, "approved_by": "ops", "note": "not now"},
    )
    assert response.status_code == 200
    assert response.json()["approval_outcome"] == "rejected"
    assert len(store.erp_log) == 0


def test_http_unknown_plan_id_is_a_404(client, store):
    response = client.post("/agent/approval/PLAN-9999", json={"approved": True})
    assert response.status_code == 404


def test_http_unknown_po_and_component_are_404s(client, store):
    assert client.post("/agent/handle-event", json={"po_id": "PO-0000"}).status_code == 404
    assert (
        client.post("/agent/handle-event", json={"component_id": "COMP-NOPE"}).status_code
        == 404
    )


def test_http_handle_event_with_no_target_detects_via_monitor(client, store):
    """No component named — MONITOR's own detection picks it. Nothing is
    decided in the route; the component is read off an event MONITOR
    already emitted."""
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    response = client.post("/agent/handle-event", json={})
    assert response.status_code == 200
    assert response.json()["component_id"] == "COMP-104"


def test_http_get_run_returns_the_cached_run(client, store):
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    posted = client.post("/agent/handle-event", json={"component_id": "COMP-104"}).json()

    fetched = client.get("/agent/runs/COMP-104")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == posted["run_id"]
    assert client.get("/agent/runs/COMP-NOPE").status_code == 404


def test_environment_and_orchestrator_share_one_store(client, store):
    """A real integration defect this caught: `environment/routes.py` used to
    bind STATE by name at import, so the environment router and the
    orchestrator could serve DIFFERENT Store instances — a disruption
    injected through the environment endpoints would be invisible to the
    agent. That breaks the judge panel (item 11), which injects on one side
    and reads results on the other."""
    before = len(client.get("/inbox").json())
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    after = len(client.get("/inbox").json())
    assert after > before

    # The orchestrator must see the message the environment route just wrote.
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert any(
        e.relation == "Contradict" and e.from_node == node("tracking", "PO-7712")
        for e in run.graph.edges
    )


def test_environment_reset_endpoint(client):
    res = client.post("/environment/reset")
    assert res.status_code == 200
    assert res.json()["status"] == "reset"


def test_environment_scenario_injection_endpoints(client):
    scenarios = [
        "supplier_delay",
        "quality_fail",
        "insufficient_qty",
        "low_reliability_fastest",
        "exceeds_approval",
        "stale_erp",
        "demand_spike",
        "expedite_unavailable",
        "priority_change",
    ]
    for s in scenarios:
        res = client.post(f"/environment/inject/{s}")
        assert res.status_code == 200, f"Scenario {s} failed with {res.status_code}"
        assert res.json()["scenario"] == s
        client.post("/environment/reset")




# ==========================================================================
# The orchestrator holds no decision logic
# ==========================================================================


def test_orchestrator_contains_no_thresholds_or_comparisons_of_its_own():
    """Constraint: sequencing and the ERP-write guard, nothing more. The only
    conditionals permitted are the write guard itself and None-handling."""
    source = open("app/api/routes.py", encoding="utf-8").read()
    for banned in (
        "days_of_coverage", "reliability_score", "quality_score",
        "min_order_quantity", "safety_stock", "_dominates", "allocate_stock",
        "quote_valid_hours",
    ):
        assert banned not in source, f"orchestrator re-implements {banned}"


def test_orchestrator_does_not_decide_execute_or_escalate():
    """It reads `brief.decision`; it never computes one."""
    source = open("app/api/routes.py", encoding="utf-8").read()
    assert "_evaluate_triggers" not in source
    assert "triggers_fired.append" not in source
    assert 'brief.decision == "execute"' in source  # reads, does not decide


def test_orchestrator_does_not_mark_pos_delayed(store):
    """Deliberately out of scope — it is an ERP write, and it is item 8's
    question. See the module docstring.

    The docstring discusses `mark_po_delayed` by name (explaining why it is
    NOT emitted), so the structural check looks for the action being USED as
    a value, not merely mentioned in prose."""
    source = open("app/api/routes.py", encoding="utf-8").read()
    assert '"mark_po_delayed"' not in source
    assert "action=mark_po_delayed" not in source

    _elicit_claim(store)
    run_pipeline(store, component_id="COMP-104", now=NOW)
    assert store.purchase_orders["PO-7712"].status == "in_transit"


def test_no_llm_in_the_orchestrator():
    source = open("app/api/routes.py", encoding="utf-8").read()
    assert "gemini" not in source.lower()
    assert "from google" not in source


# ==========================================================================
# Both LLM modes
# ==========================================================================


def test_pipeline_decision_is_identical_in_both_llm_modes(store, monkeypatch):
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    _elicit_claim(store)
    off = run_pipeline(store, component_id="COMP-104", now=NOW)

    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()
    on_store = seed_data.STATE

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
    _elicit_claim(on_store)
    on = run_pipeline(on_store, component_id="COMP-104", now=NOW)

    assert off.decision == on.decision
    assert off.brief.triggers_fired == on.brief.triggers_fired
    assert len(off.erp_writes) == len(on.erp_writes)
    assert [(e.relation, e.from_node, e.to_node) for e in off.graph.edges] == [
        (e.relation, e.from_node, e.to_node) for e in on.graph.edges
    ]


# ==========================================================================
# Reset — the judge panel's clean slate
# ==========================================================================


def test_reset_clears_the_run_cache_and_write_registry(store):
    _elicit_claim(store)
    first = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN

    reset_orchestrator_state()
    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    fresh_store = seed_data.STATE

    _elicit_claim(fresh_store)
    second = run_pipeline(fresh_store, component_id="COMP-104", now=NOW)

    # A genuinely new run writes again — fresh store, fresh write set.
    assert len(fresh_store.erp_log) == _EXPECTED_WRITES_PER_EXECUTED_PLAN
    assert second.run_id == first.run_id  # sequence reset too


def test_http_reset_endpoint(client, store):
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    client.post("/agent/handle-event", json={"component_id": "COMP-104"})
    assert client.get("/agent/runs/COMP-104").status_code == 200

    assert client.post("/agent/reset").status_code == 200
    assert client.get("/agent/runs/COMP-104").status_code == 404


# ==========================================================================
# create_alternate_po per split (signed off) — the execute path's real
# procurement, not just a decision record
# ==========================================================================


def test_execute_creates_exactly_one_po_per_split_with_correct_fields(store):
    """Two splits -> exactly two create_alternate_po actions, each carrying
    that split's own supplier, quantity, price, and an expected_delivery of
    sim-clock-now + THAT supplier's lead_time_days."""
    from datetime import timedelta

    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert run.decision == "execute"

    splits = run.brief.chosen_plan.purchase_actions()
    assert len(splits) == _EXPECTED_SPLITS

    created = [w for w in store.erp_log if w.action == "create_alternate_po"]
    assert len(created) == _EXPECTED_SPLITS

    by_supplier = {w.resulting_state["supplier_id"]: w.resulting_state for w in created}
    for split in splits:
        state = by_supplier[split.supplier_id]
        assert state["quantity"] == split.qty
        assert state["unit_price"] == split.unit_price
        assert state["component_id"] == "COMP-104"
        # sim-clock now + this supplier's own lead time, not a shared date
        expected = (NOW + timedelta(days=split.lead_time_days)).date().isoformat()
        assert state["expected_delivery"] == expected

    # The two suppliers have different lead times, so the dates must differ —
    # proof the per-split lead time was used rather than one blanket value.
    assert len({s["expected_delivery"] for s in by_supplier.values()}) == _EXPECTED_SPLITS


def test_created_po_ids_never_collide_with_a_reserved_anchor(store):
    """ARCHITECTURE.md §8's standing invariant. PO ids are generated by
    `Store.update_erp`'s existing anchor-safe range (item 1) — this module
    passes no `po_id`, so the range is reused rather than hand-rolled, which
    is the bug class §8 records as already hit once."""
    from app.environment.seed_data import _RESERVED_ANCHOR_IDS

    _elicit_claim(store)
    run_pipeline(store, component_id="COMP-104", now=NOW)

    created = {
        w.resulting_state["po_id"]
        for w in store.erp_log
        if w.action == "create_alternate_po"
    }
    assert created
    assert not (created & _RESERVED_ANCHOR_IDS["po_id"])
    # And they are real, retrievable POs, not just log rows.
    for po_id in created:
        assert store.get_purchase_order(po_id) is not None


def test_the_orchestrator_does_not_hand_roll_po_ids():
    """Structural: no `po_id` in the create_alternate_po payload, and no
    id-range arithmetic anywhere in this module."""
    source = open("app/api/routes.py", encoding="utf-8").read()
    assert '"po_id":' not in source
    assert "9000 +" not in source
    assert "PO-" not in source.split('"""')[-1]  # no PO id literal in code


def test_no_approval_ceiling_is_passed_per_po(store):
    """RATCHET already authorised the plan's total cost; a per-PO ceiling
    would be a second, redundant gate on a decision already made.

    Scoped to `_write_erp_once` specifically — the function that builds a
    NEW alternate PO's payload — not the whole file. `approval_required_above`
    now legitimately appears elsewhere in `routes.py` (`_disrupted_po_id`,
    `_current_approval_threshold`) for a different, correct reason: READING
    an EXISTING PO's own threshold to decide whether to escalate, which is
    not the "set a ceiling on the new PO being created" question this test
    guards against."""
    import inspect

    from app.api.routes import _write_erp_once

    source = inspect.getsource(_write_erp_once)
    assert "approval_required_above" not in source


def test_disrupted_pos_own_approval_threshold_is_what_ratchet_actually_checks(store, monkeypatch):
    """§5.2's `approval_required_above` is a PER-PO field; PO-7712's own
    value must be what RATCHET checks, not `config.TRACE_APPROVAL_THRESHOLD`
    — proven in both directions, so this cannot pass by coincidentally
    falling back to the global constant either way.

    PO-7712 must actually be `delayed` for `find_disrupted_po` to name it —
    `_elicit_claim` alone (the pattern most other tests in this file use)
    deliberately leaves it `in_transit` (see
    `test_orchestrator_does_not_mark_pos_delayed`), so this test sets the
    status directly, mirroring what `POST /environment/inject/supplier_delay`
    does in the real environment.
    """
    _elicit_claim(store)
    store.purchase_orders["PO-7712"].status = "delayed"

    # Direction 1: the PO's own threshold (100,000) sits BELOW the real
    # COMP-104 plan cost (~123,674), while the global default (150,000)
    # sits ABOVE it. If the global constant governed, this would EXECUTE;
    # the PO's own field must force ESCALATE instead.
    store.purchase_orders["PO-7712"].approval_required_above = 100_000.0
    run_low = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert run_low.decision == "escalate"
    assert "cost_above_threshold" in run_low.brief.triggers_fired
    assert run_low.brief.approval_threshold == 100_000.0
    assert run_low.brief.total_cost > 100_000.0

    reset_orchestrator_state()
    reset_plan_sequence()

    # Direction 2: the PO's own threshold (200,000) sits ABOVE the plan
    # cost, while the global default is monkeypatched BELOW it (60,000 —
    # `_force_escalate`'s own value, already proven elsewhere in this file
    # to trip `cost_above_threshold` on the real plan). If the global
    # constant governed, this would ESCALATE; the PO's own field must let
    # it EXECUTE.
    store.purchase_orders["PO-7712"].approval_required_above = 200_000.0
    _force_escalate(monkeypatch)
    run_high = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert run_high.decision == "execute"
    assert run_high.brief.triggers_fired == []
    assert run_high.brief.approval_threshold == 200_000.0
    assert run_high.awaiting_approval is False


def test_provenance_cites_every_created_po(store):
    """The audit trail must show not just what was decided but what was
    actually written — the one thing that cannot be taken back."""
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    created = [
        w.resulting_state["po_id"]
        for w in store.erp_log
        if w.action == "create_alternate_po"
    ]
    for po_id in created:
        edges = [
            e for e in run.graph.edges
            if e.relation == "Trigger" and e.to_node == node("po", po_id)
        ]
        assert len(edges) == 1, f"{po_id} not cited exactly once in the trail"
        assert edges[0].from_node == node("plan", run.plan_id)
        assert "IRREVERSIBLE" in edges[0].note

    # The store_plan row is cited too, so the write set is complete.
    assert any(e.to_node.startswith("erp_write:") for e in run.graph.edges)
    assert run.graph.unknown_node_kinds() == []
    assert run.graph.duplicate_edges() == []


def test_provenance_gains_the_po_edges_on_approval_not_before(store, monkeypatch):
    """An escalated plan's graph is built BEFORE the write. When approval
    later performs it, the trail must gain those edges — otherwise the audit
    trail of an approved plan silently omits the POs it created."""
    _force_escalate(monkeypatch)
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert not [e for e in run.graph.edges if e.to_node.startswith("po:PO-9")]

    resolve_approval(store, plan_id=run.plan_id, approved=True)

    po_edges = [e for e in run.graph.edges if e.to_node.startswith("po:PO-9")]
    assert len(po_edges) == _EXPECTED_SPLITS
    assert run.graph.duplicate_edges() == []

    # Appending is idempotent — a second approval must not duplicate edges.
    resolve_approval(store, plan_id=run.plan_id, approved=True)
    assert len([e for e in run.graph.edges if e.to_node.startswith("po:PO-9")]) == (
        _EXPECTED_SPLITS
    )
    assert run.graph.duplicate_edges() == []


def test_created_pos_become_dependable_inbound_for_the_next_coverage_pass(store):
    """A created PO is `pending`, which IS a dependable inbound status — so
    the next coverage pass credits it. That is what makes the executed plan
    actually change the coverage picture rather than only being recorded."""
    _elicit_claim(store)
    run_pipeline(store, component_id="COMP-104", now=NOW)

    created = [
        w.resulting_state["po_id"]
        for w in store.erp_log
        if w.action == "create_alternate_po"
    ]
    coverage = compute_coverage(store, now=NOW)
    inbound = {
        po_id
        for result in coverage.results
        if result.component_id == "COMP-104"
        for po_id in result.inbound_po_ids
    }
    for po_id in created:
        assert po_id in inbound
