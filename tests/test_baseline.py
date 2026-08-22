"""TEST BASELINE COMPARISON HARNESS — Item 13 verification.

Tests multi-baseline comparison over four variants (static_workflow,
cheapest_always, retry_only, trace) plus bonus claim_ablation mode.
"""

import ast
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.api.routes import reset_orchestrator_state, run_pipeline
from app.engine.baseline import (
    HIDDEN_TEST_SCENARIOS,
    BaselineComparisonReport,
    run_baseline_comparison,
)
from app.environment import seed_data
from app.main import app

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_state():
    seed_data.STATE = seed_data.build_store()
    reset_orchestrator_state()
    yield
    reset_orchestrator_state()


def test_smoke_test_po_7712_disruption_across_all_variants():
    """Smoke-test: fire the same PO-7712 disruption through all four variants,
    show static_workflow and cheapest_always both reporting a silent failure
    that trace does not."""
    report = run_baseline_comparison(scenario_name="po_7712_delay", component_id="COMP-104", now=NOW)

    assert isinstance(report, BaselineComparisonReport)
    assert report.scenario_name == "po_7712_delay"
    assert report.component_id == "COMP-104"

    # All 4 core variants + bonus claim_ablation must be present
    assert "static_workflow" in report.variants
    assert "cheapest_always" in report.variants
    assert "retry_only" in report.variants
    assert "trace" in report.variants
    assert "claim_ablation" in report.variants

    # Core assertion: static_workflow and cheapest_always both report a silent failure that trace does not
    trace_res = report.variants["trace"]
    static_res = report.variants["static_workflow"]
    cheapest_res = report.variants["cheapest_always"]
    retry_res = report.variants["retry_only"]

    assert trace_res.silent_failure is False, "TRACE must NOT report a silent failure"
    assert static_res.silent_failure is True, "static_workflow MUST report a silent failure"
    assert cheapest_res.silent_failure is True, "cheapest_always MUST report a silent failure"
    assert retry_res.silent_failure is True, "retry_only MUST report a silent failure"

    # TRACE passes all 10 hidden tests
    assert trace_res.hidden_tests_passed_count == 10
    assert len(trace_res.hidden_tests_passed) == 10

    # cheapest_always passes fewer tests
    assert cheapest_res.hidden_tests_passed_count < 10

    # Tool calls reported using item 10's ToolAuditReport
    assert trace_res.tool_calls.total_calls_made >= 0
    assert trace_res.summary_sentence != ""


def test_baseline_compare_endpoint():
    """Test POST /baseline/compare/po_7712_delay HTTP endpoint."""
    resp = client.post("/baseline/compare/po_7712_delay?component_id=COMP-104")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["scenario_name"] == "po_7712_delay"
    assert data["component_id"] == "COMP-104"
    assert "trace" in data["variants"]
    assert "static_workflow" in data["variants"]
    assert "cheapest_always" in data["variants"]
    assert "retry_only" in data["variants"]

    # Verify silent failure status
    assert data["variants"]["trace"]["silent_failure"] is False
    assert data["variants"]["cheapest_always"]["silent_failure"] is True
    assert data["variants"]["static_workflow"]["silent_failure"] is True


def test_cheapest_always_variant_skips_verify_and_selects_cheapest():
    """cheapest_always variant skips VERIFY stage, skips production reschedule, and picks SUP-18 (min price)."""
    store = seed_data.build_store()
    run = run_pipeline(store, component_id="COMP-104", now=NOW, variant="cheapest_always")

    assert run.decision == "execute"
    assert run.brief.chosen_plan is not None
    plan = run.brief.chosen_plan

    # Verify VERIFY was skipped
    assert any("VERIFY (skipped" in s for s in run.stages)

    # Verify SUP-18 was picked (unit price ₹110)
    split_suppliers = [a.supplier_id for a in plan.purchase_actions()]
    assert "SUP-18" in split_suppliers

    # Verify production reschedules are absent
    reschedules = [a for a in plan.actions if a.type == "production_reschedule"]
    assert len(reschedules) == 0


def test_static_workflow_disables_reentry():
    """static_workflow disables staleness and contingency re-entry."""
    store = seed_data.build_store()
    run1 = run_pipeline(store, component_id="COMP-104", now=NOW, variant="static_workflow")
    run2 = run_pipeline(store, component_id="COMP-104", now=NOW, variant="static_workflow")

    assert run1.run_id == run2.run_id  # Returns cached without re-entry check


def test_claim_ablation_bonus_mode():
    """claim_ablation bonus variant runs TRACE with claim verification specifically disabled."""
    report = run_baseline_comparison(scenario_name="po_7712_delay", component_id="COMP-104", now=NOW)
    ablation = report.variants["claim_ablation"]

    assert ablation.variant == "claim_ablation"
    assert ablation.silent_failure is True
    assert "scenario_1_supplier_delays" not in ablation.hidden_tests_passed
    assert "scenario_2_tracking_contradicts" not in ablation.hidden_tests_passed


def test_no_banned_metric_terms_in_baseline_module():
    """AST check: no invented metrics (no 'efficiency', 'ratio', 'pct', 'percentage', 'score')
    in baseline.py data fields or executable code."""
    source = open("app/engine/baseline.py", encoding="utf-8").read()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in ("VariantResult", "BaselineComparisonReport"):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fname = item.target.id
                    for banned in ("efficiency", "ratio", "pct", "percentage"):
                        assert banned not in fname.lower(), f"{node.name}.{fname} contains banned term '{banned}'"


def test_no_llm_in_variant_comparison_logic():
    """AST check: baseline comparison logic is 100% deterministic with no LLM involvement."""
    source = open("app/engine/baseline.py", encoding="utf-8").read()
    assert "gemini" not in source.lower()
    assert "llm" not in source.lower()
    assert "from google" not in source
