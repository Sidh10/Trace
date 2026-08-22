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
    assert len(run.erp_writes) == 1
    assert len(store.erp_log) == 1
    assert run.erp_writes[0].action == "store_plan"
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
    assert len(store.erp_log) == 1, f"expected exactly 1 ERP write, got {len(store.erp_log)}"
    assert len(first.erp_writes) == 1
    assert len(second.erp_writes) == 1
    assert first.erp_writes[0].update_id == second.erp_writes[0].update_id


def test_approving_twice_produces_exactly_one_erp_write(store, monkeypatch):
    _force_escalate(monkeypatch)
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == 0

    first = resolve_approval(store, plan_id=run.plan_id, approved=True)
    second = resolve_approval(store, plan_id=run.plan_id, approved=True)

    assert len(store.erp_log) == 1
    assert len(first.erp_writes) == 1
    assert len(second.erp_writes) == 1
    assert first.erp_writes[0].update_id == second.erp_writes[0].update_id


def test_approval_after_an_execute_does_not_write_a_second_time(store):
    """The two paths into the write must share one guard, not two."""
    _elicit_claim(store)
    run = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert len(store.erp_log) == 1

    resolve_approval(store, plan_id=run.plan_id, approved=True)
    assert len(store.erp_log) == 1


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


def test_there_is_exactly_one_erp_write_call_site_in_the_orchestrator():
    source = open("app/api/routes.py", encoding="utf-8").read()
    assert source.count("store.update_erp(") == 1


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
    assert len(body["erp_writes"]) == 1
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

    assert http_edges == direct_edges
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
    assert len(first.json()["erp_writes"]) == 1

    second = client.post(f"/agent/approval/{plan_id}", json={"approved": True})
    assert second.status_code == 200
    assert len(second.json()["erp_writes"]) == 1
    assert len(store.erp_log) == 1


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
    assert len(store.erp_log) == 1

    reset_orchestrator_state()
    clock.reset()
    seed_data.STATE = seed_data.build_store()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    fresh_store = seed_data.STATE

    _elicit_claim(fresh_store)
    second = run_pipeline(fresh_store, component_id="COMP-104", now=NOW)

    assert len(fresh_store.erp_log) == 1  # a genuinely new run writes again
    assert second.run_id == first.run_id  # sequence reset too


def test_http_reset_endpoint(client, store):
    client.post("/suppliers/SUP-21/message", json=_ELICIT_CLAIM)
    client.post("/agent/handle-event", json={"component_id": "COMP-104"})
    assert client.get("/agent/runs/COMP-104").status_code == 200

    assert client.post("/agent/reset").status_code == 200
    assert client.get("/agent/runs/COMP-104").status_code == 404
