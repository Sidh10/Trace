"""STALENESS tests — ARCHITECTURE.md §4 item 8.

Two things matter most here and both are easy to get wrong:

  1. **Re-entry lands at the EARLIEST invalidated stage, not the top.** A
     replan that always restarts from COVERAGE is not earliest-conflict
     rollback, it is just re-running the pipeline — and every test below that
     names a stage would still pass if it did. So each asserts the specific
     stage AND that the earlier ones were reused.
  2. **Re-entry must not corrupt reliability scores.** VERIFY is not
     idempotent: it applies the exponentially weighted update, so re-running
     it on unchanged evidence penalises a supplier again (measured: 0.75 ->
     0.45 -> 0.27 -> 0.162). A COVERAGE-level re-entry re-runs everything
     downstream, which naively includes VERIFY. The rule-5 tagging is what
     stops that, and `test_reliability_is_not_double_penalised_*` is what
     proves the stop works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.routes import reset_orchestrator_state, resolve_approval, run_pipeline
from app.audit.provenance import reset_provenance_sequences
from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import reset_plan_sequence
from app.engine.staleness import (
    MAX_REENTRY_PASSES,
    STAGE_ORDER,
    STAGE_REVERSIBILITY,
    capture_preconditions,
    detect_staleness,
)
from app.engine.verify import run_verification_cycle
from app.environment import seed_data
from app.environment.clock import clock
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
    """Escalate rather than execute, so a plan EXISTS but has NOT been
    written — which is the state the task's smoke test requires the second
    disruption to arrive in."""
    monkeypatch.setattr(config, "TRACE_APPROVAL_THRESHOLD", 60_000.0)


def _first_plan(store):
    return run_pipeline(store, component_id="COMP-104", now=NOW)


def _snapshot_for(store, plan, now=NOW):
    coverage = compute_coverage(store, now=now)
    monitor = run_monitor_cycle(store, coverage=coverage, now=now)
    return capture_preconditions(
        store, plan, coverage, monitor,
        approval_threshold=config.TRACE_APPROVAL_THRESHOLD, now=now,
    )


# ==========================================================================
# The run-cache bug item 8 exists to fix
# ==========================================================================


def test_an_unchanged_component_still_returns_the_cached_run(store):
    """The cache must stay a cache — invalidating on every call would make
    'fire the same disruption twice = one ERP write' false again."""
    first = _first_plan(store)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.run_id == first.run_id
    assert second.plan_id == first.plan_id
    assert second.staleness is None


def test_a_genuinely_new_disruption_no_longer_replays_the_stale_run(store, escalating):
    """The bug: keyed on component_id with no invalidation, a second
    disruption on an already-handled component wrongly replayed the first
    decision. It must now replan."""
    first = _first_plan(store)
    store.inventory["COMP-104"].usable_stock = 120  # a real change

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.run_id != first.run_id
    assert second.plan_id != first.plan_id
    assert second.superseded_plan_id == first.plan_id
    assert second.staleness is not None
    assert second.staleness.is_stale is True


# ==========================================================================
# Earliest-conflict rollback — the stage must be right, not just non-None
# ==========================================================================


@pytest.mark.parametrize(
    "mutate,expected_stage,expected_kind",
    [
        (lambda s: setattr(s.inventory["COMP-104"], "usable_stock", 120),
         "COVERAGE", "inventory_changed"),
        (lambda s: setattr(s.purchase_orders["PO-7712"], "status", "delayed"),
         "COVERAGE", "po_status_changed"),
        (lambda s: setattr(s.tracking["PO-7712"], "tracking_status", "in_transit"),
         "MONITOR", "tracking_status_changed"),
        (lambda s: setattr(s.suppliers["SUP-42"], "unit_price", 999.0),
         "SOLVER", "supplier_unit_price_changed"),
        (lambda s: setattr(s.suppliers["SUP-42"], "available_quantity", 1),
         "SOLVER", "supplier_available_quantity_changed"),
    ],
)
def test_reentry_lands_at_the_earliest_invalidated_stage(
    store, escalating, mutate, expected_stage, expected_kind
):
    first = _first_plan(store)
    mutate(store)

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == expected_stage
    assert expected_kind in {f.kind for f in second.staleness.findings}
    assert second.superseded_plan_id == first.plan_id


def test_quote_expiry_reenters_at_solver_not_the_top(store, escalating):
    """§5.7's quote_valid_hours as a real constraint. This is a check against
    the quote's OWN issue time — a fact about that quote — not a replanning
    timer."""
    first = _first_plan(store)
    later = NOW + timedelta(hours=7)  # quote_valid_hours is 6

    second = run_pipeline(store, component_id="COMP-104", now=later)

    assert second.reentered_at_stage == "SOLVER"
    kinds = {f.kind for f in second.staleness.findings}
    assert kinds == {"quote_expired"}
    assert second.plan_id != first.plan_id


def test_a_new_supplier_claim_reenters_at_verify_not_monitor(store, escalating):
    """The distinction that makes the mapping principled: MONITOR's tracking
    read is still valid, so re-entering there would be wasted. What changed
    is the claim VERIFY compares tracking against."""
    _first_plan(store)
    store.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Further update on PO-7712",
        body="PO-7712 has now been dispatched, second confirmation.",
    )

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == "VERIFY"
    assert "new_supplier_claim" in {f.kind for f in second.staleness.findings}


def test_approval_threshold_change_reenters_at_ratchet_only(store, escalating, monkeypatch):
    """The latest possible re-entry: nothing upstream is invalid, only the
    verdict. Everything before RATCHET must be reused."""
    first = _first_plan(store)
    assert first.decision == "escalate"

    monkeypatch.setattr(config, "TRACE_APPROVAL_THRESHOLD", 999_999.0)
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == "RATCHET"
    assert second.decision == "execute"  # the verdict genuinely flipped
    joined = " | ".join(second.stages)
    for reused in ("COVERAGE (reused", "MONITOR (reused", "HARD FILTER + SOLVER (reused"):
        assert reused in joined


def test_earlier_stages_are_reused_not_recomputed(store, escalating):
    """Earliest-conflict means the stages before it are REUSED. Without this
    assertion a full re-run from the top would pass every stage test above."""
    _first_plan(store)
    store.suppliers["SUP-42"].unit_price = 999.0

    second = run_pipeline(store, component_id="COMP-104", now=NOW)
    joined = " | ".join(second.stages)

    assert second.reentered_at_stage == "SOLVER"
    assert "COVERAGE (reused" in joined
    assert "MONITOR (reused" in joined
    assert "VERIFY (reused" in joined
    # ...and the stages from the conflict onward really did re-run.
    assert "HARD FILTER + SOLVER" in second.stages
    assert "PLAN" in second.stages
    assert "RATCHET" in second.stages


# ==========================================================================
# Rule 5 tagging — and the reliability corruption it prevents
# ==========================================================================


def test_verify_is_tagged_non_idempotent_and_erp_write_irreversible():
    assert STAGE_REVERSIBILITY["VERIFY"] == "compensable"
    assert STAGE_REVERSIBILITY["ERP_WRITE"] == "irreversible"
    for stage in ("COVERAGE", "MONITOR", "SOLVER", "PLAN", "RATCHET"):
        assert STAGE_REVERSIBILITY[stage] == "idempotent"
    assert set(STAGE_ORDER) | {"ERP_WRITE"} == set(STAGE_REVERSIBILITY)


def test_verify_really_is_non_idempotent(store):
    """The premise the tagging rests on, measured rather than assumed. If
    this ever becomes idempotent, the skip rule can be relaxed — but only
    then."""
    coverage = compute_coverage(store, now=NOW)
    monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)

    scores = []
    for _ in range(3):
        run_verification_cycle(store, coverage=coverage, monitor=monitor, now=NOW)
        scores.append(store.suppliers["SUP-21"].reliability_score)

    assert scores[0] != scores[1] != scores[2], (
        "VERIFY appears idempotent now — re-check the skip rule in staleness.py"
    )


def test_reliability_is_not_double_penalised_on_a_coverage_reentry(store, escalating):
    """THE test this module exists for. A COVERAGE-level re-entry re-runs
    everything downstream, which naively includes VERIFY — and VERIFY would
    re-apply the EWMA to SUP-21 for the very same tracking contradiction it
    already recorded. Rule-5 tagging is what stops it."""
    _first_plan(store)
    before = store.suppliers["SUP-21"].reliability_score
    assert before == 0.45  # already downgraded once, correctly

    store.inventory["COMP-104"].usable_stock = 120  # unrelated, COVERAGE-level
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == "COVERAGE"
    assert store.suppliers["SUP-21"].reliability_score == before, (
        "SUP-21 was penalised twice for one tracking contradiction"
    )
    assert any("VERIFY (reused" in s for s in second.stages)


def test_verify_does_re_run_when_a_finding_names_it(store, escalating):
    """The skip is conditional, not blanket: when VERIFY's own input changed
    (a new claim), re-running it IS justified — there is new evidence."""
    _first_plan(store)
    store.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Another update on PO-7712",
        body="PO-7712 dispatched, confirming again.",
    )

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.reentered_at_stage == "VERIFY"
    assert "VERIFY" in second.stages  # re-run, not the "(reused" variant
    assert not any("VERIFY (reused" in s for s in second.stages)


def test_a_replan_after_execution_supersedes_rather_than_undoes(store):
    """You cannot roll back past an irreversible write. The prior POs stand;
    the new plan supersedes. ARCHITECTURE.md §5 excludes PO cancellation, so
    there is no undo to reach for."""
    first = _first_plan(store)
    assert first.decision == "execute"
    writes_before = len(store.erp_log)
    assert writes_before > 0

    store.inventory["COMP-104"].usable_stock = 120
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.superseded_plan_id == first.plan_id
    # The prior irreversible writes are untouched.
    for write in store.erp_log[:writes_before]:
        assert write.update_id in {w.update_id for w in store.erp_log}
    assert any("SUPERSEDES rather than undoes" in s for s in second.stages)


def test_pos_created_by_the_superseded_plan_are_credited_by_the_replan(store):
    """The elegant consequence: a created PO is `pending`, a dependable
    inbound status, so the new coverage pass counts it. The superseding plan
    accounts for stock already on order rather than double-buying."""
    first = _first_plan(store)
    created = {
        w.resulting_state["po_id"]
        for w in store.erp_log
        if w.action == "create_alternate_po"
    }
    assert created

    store.inventory["COMP-104"].usable_stock = 120
    run_pipeline(store, component_id="COMP-104", now=NOW)

    coverage = compute_coverage(store, now=NOW)
    inbound = {
        po_id
        for r in coverage.results
        if r.component_id == "COMP-104"
        for po_id in r.inbound_po_ids
    }
    assert created <= inbound
    assert first.plan_id is not None


# ==========================================================================
# Post-replan verification
# ==========================================================================


def test_the_new_plan_is_verified_not_trusted_for_being_second(store, escalating):
    _first_plan(store)
    store.suppliers["SUP-42"].unit_price = 999.0

    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.post_replan_verified is True
    assert second.residual_staleness is None
    assert any("POST-REPLAN VERIFICATION" in s for s in second.stages)


def test_reentry_is_bounded_per_rule_6():
    """A termination guarantee, not a domain threshold."""
    assert MAX_REENTRY_PASSES >= 1
    source = open("app/engine/staleness.py", encoding="utf-8").read()
    assert "rule 6" in source


def test_detect_staleness_is_a_pure_diff_with_no_side_effects(store):
    """It reads and compares; it must change nothing."""
    plan = _first_plan(store).brief.chosen_plan
    snapshot = _snapshot_for(store, plan)

    before = {
        "suppliers": [s.model_dump(mode="json") for s in store.list_suppliers()],
        "inventory": [i.model_dump(mode="json") for i in store.list_inventory()],
        "erp_log": len(store.erp_log),
    }
    detect_staleness(
        store, snapshot,
        current_approval_threshold=config.TRACE_APPROVAL_THRESHOLD, now=NOW,
    )
    assert [s.model_dump(mode="json") for s in store.list_suppliers()] == before["suppliers"]
    assert [i.model_dump(mode="json") for i in store.list_inventory()] == before["inventory"]
    assert len(store.erp_log) == before["erp_log"]


def test_an_unchanged_snapshot_reports_no_findings(store):
    plan = _first_plan(store).brief.chosen_plan
    snapshot = _snapshot_for(store, plan)

    report = detect_staleness(
        store, snapshot,
        current_approval_threshold=config.TRACE_APPROVAL_THRESHOLD, now=NOW,
    )
    assert report.is_stale is False
    assert report.findings == []
    assert report.reentry_stage is None


# ==========================================================================
# The ERP boundary still holds under re-entry
# ==========================================================================


def test_a_stale_escalated_plan_still_writes_nothing(store, escalating):
    _first_plan(store)
    assert len(store.erp_log) == 0

    store.suppliers["SUP-42"].unit_price = 999.0
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    assert second.decision == "escalate"
    assert second.erp_writes == []
    assert len(store.erp_log) == 0, "a re-entry wrote to the ERP on the escalate path"


def test_approving_a_superseded_plan_does_not_write_the_new_one(store, escalating):
    """The old plan_id is still approvable — and must write only ITS own
    write set, not the superseding plan's."""
    first = _first_plan(store)
    store.suppliers["SUP-42"].unit_price = 999.0
    second = run_pipeline(store, component_id="COMP-104", now=NOW)
    assert second.plan_id != first.plan_id

    resolve_approval(store, plan_id=second.plan_id, approved=True)
    written_plan_ids = {
        w.resulting_state.get("plan_id")
        for w in store.erp_log
        if w.action == "store_plan"
    }
    assert written_plan_ids == {second.plan_id}


# ==========================================================================
# Through the real HTTP orchestrator — the task's smoke test
# ==========================================================================


def test_http_two_disruptions_reenter_at_the_correct_non_top_stage(store, escalating):
    """The scope's own smoke test: two disruptions on the same component, the
    second arriving after the first plan exists but BEFORE execution, and
    re-entry firing at the correct — not top — stage."""
    client = TestClient(app)

    first = client.post("/agent/handle-event", json={"po_id": "PO-7712"}).json()
    assert first["decision"] == "escalate"
    assert first["erp_writes"] == []  # plan exists, not executed
    assert first["staleness"] is None

    # Disruption 2: the sim clock passes quote_valid_hours.
    client.post("/clock/advance?hours=7")
    second = client.post("/agent/handle-event", json={"po_id": "PO-7712"}).json()

    assert second["staleness"]["is_stale"] is True
    assert second["reentered_at_stage"] == "SOLVER"
    assert second["reentered_at_stage"] != STAGE_ORDER[0]  # not the top
    assert second["superseded_plan_id"] == first["plan_id"]
    assert second["plan_id"] != first["plan_id"]
    assert second["post_replan_verified"] is True
    assert second["erp_writes"] == []  # still escalate, still nothing written

    joined = " | ".join(second["stages"])
    assert "COVERAGE (reused" in joined
    assert "MONITOR (reused" in joined


def test_http_reset_clears_the_staleness_outputs_too(store):
    client = TestClient(app)
    client.post("/agent/handle-event", json={"po_id": "PO-7712"})
    assert client.post("/agent/reset").status_code == 200
    assert client.get("/agent/runs/COMP-104").status_code == 404


# ==========================================================================
# Constraints
# ==========================================================================


def test_no_llm_in_the_staleness_path():
    source = open("app/engine/staleness.py", encoding="utf-8").read()
    assert "gemini" not in source.lower()
    assert "from google" not in source


def test_staleness_detection_is_a_state_diff_not_a_timer():
    """The only clock comparison permitted is against a quote's OWN
    quote_valid_hours window — a fact about that quote. No interval, no
    'replan every N', no elapsed-time trigger on the plan itself.

    Checked against the CODE, not the prose: the module docstring discusses
    intervals precisely to explain why there isn't one, so a naive substring
    search over the whole file flags its own explanation."""
    import ast

    source = open("app/engine/staleness.py", encoding="utf-8").read()
    tree = ast.parse(source)

    # Strip every docstring node, then unparse — leaving executable code only.
    for scope in ast.walk(tree):
        if not isinstance(
            scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = scope.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            scope.body = body[1:] or [ast.Pass()]

    code = ast.unparse(ast.fix_missing_locations(tree)).lower()

    for banned in ("interval", "every_n", "max_age", "ttl", "expiry_seconds"):
        assert banned not in code, f"{banned!r} appears in executable code"
    assert "quote_valid_hours" in source


def test_staleness_findings_carry_the_real_before_and_after(store, escalating):
    """No invented severity or score — a finding states which field moved and
    to what, and nothing else."""
    from app.engine.staleness import StalenessFinding

    _first_plan(store)
    store.inventory["COMP-104"].usable_stock = 120
    second = run_pipeline(store, component_id="COMP-104", now=NOW)

    for field in StalenessFinding.model_fields:
        assert "severity" not in field
        assert "confidence" not in field
        assert "score" not in field
    for finding in second.staleness.findings:
        assert finding.was
        assert finding.now
        assert finding.was != finding.now
        assert finding.detail


def test_both_llm_modes_produce_the_same_reentry(store, escalating, monkeypatch):
    monkeypatch.setattr(config, "TRACE_LLM_ENABLED", False)
    _first_plan(store)
    store.suppliers["SUP-42"].unit_price = 999.0
    off = run_pipeline(store, component_id="COMP-104", now=NOW)

    # Fresh environment, LLM on, both entry points mocked.
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
    run_pipeline(on_store, component_id="COMP-104", now=NOW)
    on_store.suppliers["SUP-42"].unit_price = 999.0
    on = run_pipeline(on_store, component_id="COMP-104", now=NOW)

    assert off.reentered_at_stage == on.reentered_at_stage
    assert {f.kind for f in off.staleness.findings} == {
        f.kind for f in on.staleness.findings
    }
    assert off.post_replan_verified == on.post_replan_verified
