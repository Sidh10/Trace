"""TOOL AUDIT tests — ARCHITECTURE.md §4 item 10.

Tests that item 10's ToolAuditReport reads per-call preconditions from
upstream reports correctly, produces an honest count-vs-necessity summary,
and never invents a metric (AGENTS.md rule 7).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.monitor import run_monitor_cycle
from app.engine.planner import reset_plan_sequence, run_planner
from app.engine.ratchet import run_ratchet
from app.engine.solver import run_solver
from app.engine.tool_audit import (
    ToolAuditReport,
    ToolCallPrecondition,
    build_tool_audit,
)
from app.engine.verify import run_verification_cycle
from app.audit.provenance import (
    build_provenance_graph,
    reset_provenance_sequences,
)
from app.environment.clock import clock
from app.environment.seed_data import build_store

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    clock.reset()
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    return build_store()


def _full_pipeline(store, now=NOW):
    """The real Beat 2/3/4 sequence, end to end, against the seeded dataset.
    Returns every report the audit builder consumes."""
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


# =========================================================================
# Precondition completeness
# =========================================================================


class TestMonitorPreconditions:
    """Every polled PO has a precondition citing load_bearing; every skipped
    PO has one citing not_load_bearing."""

    def test_polled_pos_have_load_bearing_preconditions(self, store):
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        monitor_preconditions = [p for p in audit.preconditions if p.module == "monitor"]

        polled_decisions = monitor.polled()
        for decision in polled_decisions:
            matching = [p for p in monitor_preconditions if p.subject == decision.po_id]
            assert len(matching) == 1, f"Expected exactly one precondition for {decision.po_id}"
            p = matching[0]
            assert p.called is True
            assert "load_bearing" in p.precondition

    def test_skipped_pos_have_not_load_bearing_preconditions(self, store):
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        monitor_preconditions = [p for p in audit.preconditions if p.module == "monitor"]

        skipped_decisions = monitor.skipped()
        for decision in skipped_decisions:
            matching = [p for p in monitor_preconditions if p.subject == decision.po_id]
            assert len(matching) == 1, f"Expected exactly one precondition for {decision.po_id}"
            p = matching[0]
            assert p.called is False
            assert p.precondition == "not_load_bearing"

    def test_every_decision_has_a_precondition(self, store):
        """No PollDecision should be missing from the audit."""
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        monitor_preconditions = [p for p in audit.preconditions if p.module == "monitor"]
        assert len(monitor_preconditions) == len(monitor.decisions)


# =========================================================================
# Verify preconditions
# =========================================================================


class TestVerifyPreconditions:
    """Reused probes, own probes, and skips are all logged."""

    def test_probe_source_recorded(self, store):
        store.send_supplier_message(
            supplier_id="SUP-21", to="supplier21@example.com", subject="x",
            body="Any update on PO-7712?",
        )
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        verification = run_verification_cycle(
            store, coverage=coverage, monitor=monitor, now=NOW,
        )
        audit = build_tool_audit(verification=verification, now=NOW)

        verify_preconditions = [p for p in audit.preconditions if p.module == "verify"]
        # Every verification + skip should have a precondition
        expected_count = len(verification.verifications) + len(verification.skipped)
        assert len(verify_preconditions) == expected_count

        for p in verify_preconditions:
            assert "probe_source=" in p.precondition or "skip_reason=" in p.precondition

    def test_reused_probe_not_marked_as_called(self, store):
        """When VERIFY reuses MONITOR's read, called must be False."""
        store.send_supplier_message(
            supplier_id="SUP-21", to="supplier21@example.com", subject="x",
            body="Any update on PO-7712?",
        )
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        verification = run_verification_cycle(
            store, coverage=coverage, monitor=monitor, now=NOW,
        )
        audit = build_tool_audit(verification=verification, now=NOW)

        reused = [
            p for p in audit.preconditions
            if p.module == "verify" and "reused_from_monitor" in p.precondition
        ]
        for p in reused:
            assert p.called is False, (
                f"{p.subject}: reused_from_monitor should have called=False"
            )


# =========================================================================
# Solver preconditions
# =========================================================================


class TestSolverPreconditions:
    """Hard-filtered suppliers show up as skipped calls."""

    def test_hard_filtered_suppliers_logged(self, store):
        _, _, _, solver_result, _, _ = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(solver_result=solver_result, now=NOW)

        solver_preconditions = [p for p in audit.preconditions if p.module == "solver"]
        hard_filter_reasons = {"uncertified", "quality_below_threshold", "budget_infeasible"}
        hard_filtered_rejections = [
            r for r in solver_result.rejected if r.reason in hard_filter_reasons
        ]

        # Every hard-filter rejection should produce a precondition entry
        assert len(solver_preconditions) == len(hard_filtered_rejections)
        for p in solver_preconditions:
            assert p.called is False
            assert "hard_filter_drop=" in p.precondition


# =========================================================================
# Ratchet preconditions
# =========================================================================


class TestRatchetPreconditions:
    """Approval check precondition is present when a plan exists."""

    def test_approval_check_logged_when_plan_exists(self, store):
        _, _, _, solver_result, plan, brief = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(brief=brief, now=NOW)

        ratchet_preconditions = [p for p in audit.preconditions if p.module == "ratchet"]
        assert len(ratchet_preconditions) == 1
        p = ratchet_preconditions[0]
        if brief.approval_checks_made > 0:
            assert p.called is True
            assert "plan_cost_requires_approval_check" in p.precondition
        else:
            assert p.called is False
            assert "no_plan_to_check" in p.precondition


# =========================================================================
# Summary correctness
# =========================================================================


class TestSummaryCorrectness:
    """Counts add up; module summaries match ToolCallSummary totals."""

    def test_totals_are_sum_of_module_summaries(self, store):
        _, monitor, verification, solver_result, _, brief = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(
            monitor=monitor, verification=verification,
            solver_result=solver_result, brief=brief, now=NOW,
        )

        assert audit.total_calls_made == sum(s.calls_made for s in audit.module_summaries)
        assert audit.total_calls_avoided == sum(s.calls_avoided for s in audit.module_summaries)
        assert audit.total_calls_available == sum(s.calls_available for s in audit.module_summaries)

    def test_monitor_counts_match_upstream(self, store):
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        monitor_summary = [s for s in audit.module_summaries if s.module == "monitor"][0]
        assert monitor_summary.calls_made == monitor.polls_made
        assert monitor_summary.calls_available == monitor.polls_available

    def test_verify_counts_match_upstream(self, store):
        store.send_supplier_message(
            supplier_id="SUP-21", to="supplier21@example.com", subject="x",
            body="Any update on PO-7712?",
        )
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        verification = run_verification_cycle(
            store, coverage=coverage, monitor=monitor, now=NOW,
        )
        audit = build_tool_audit(verification=verification, now=NOW)

        verify_summary = [s for s in audit.module_summaries if s.module == "verify"][0]
        assert verify_summary.calls_made == verification.probes_made
        assert verify_summary.calls_avoided == verification.probes_reused_from_monitor

    def test_necessity_verdict_is_nonempty_string(self, store):
        _, monitor, verification, solver_result, _, brief = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(
            monitor=monitor, verification=verification,
            solver_result=solver_result, brief=brief, now=NOW,
        )
        assert isinstance(audit.necessity_verdict, str)
        assert len(audit.necessity_verdict) > 10


# =========================================================================
# No invented metric — AGENTS.md rule 7
# =========================================================================


class TestNoInventedMetric:
    """No field on ToolAuditReport or its children, and no executable code in
    tool_audit.py, contains banned terms ('efficiency', 'ratio', 'pct',
    'percentage'). Uses the shared AST helper executable_source."""

    def test_no_banned_metrics_in_executable_code(self):
        from conftest import executable_source

        code = executable_source("app/engine/tool_audit.py").lower()
        for banned in ("efficiency", "ratio", "pct", "percentage"):
            assert banned not in code, f"{banned!r} appears in executable code of tool_audit.py"

    def test_no_score_or_ratio_field(self, store):
        _, monitor, verification, solver_result, _, brief = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(
            monitor=monitor, verification=verification,
            solver_result=solver_result, brief=brief, now=NOW,
        )

        banned = {"score", "efficiency", "ratio", "pct", "percentage"}
        for field_name in ToolAuditReport.model_fields:
            for word in banned:
                assert word not in field_name.lower(), (
                    f"ToolAuditReport.{field_name} contains banned word '{word}'"
                )

        from app.engine.tool_audit import ModuleCallSummary, ToolCallPrecondition as TCP
        for summary in audit.module_summaries:
            for field_name in ModuleCallSummary.model_fields:
                for word in banned:
                    assert word not in field_name.lower(), (
                        f"ModuleCallSummary.{field_name} contains banned word '{word}'"
                    )

        for p in audit.preconditions:
            for field_name in TCP.model_fields:
                for word in banned:
                    assert word not in field_name.lower(), (
                        f"ToolCallPrecondition.{field_name} contains banned word '{word}'"
                    )



# =========================================================================
# Integration with provenance graph
# =========================================================================


class TestProvenanceIntegration:
    """ToolAuditReport is properly attached to the ProvenanceGraph."""

    def test_graph_carries_tool_audit(self, store):
        _, monitor, verification, solver_result, plan, brief = _full_pipeline(store, now=NOW)
        audit = build_tool_audit(
            monitor=monitor, verification=verification,
            solver_result=solver_result, brief=brief, now=NOW,
        )
        graph = build_provenance_graph(
            monitor=monitor, verification=verification,
            solver_result=solver_result, plan=plan, brief=brief,
            tool_audit=audit, now=NOW,
        )
        assert graph.tool_audit is not None
        assert graph.tool_audit is audit
        # Item 7's ToolCallSummary is still there
        assert graph.tool_calls is not None
        assert graph.tool_calls.total_calls_made > 0

    def test_graph_without_tool_audit_still_works(self, store):
        """Backward compatible: None is the default."""
        _, monitor, verification, solver_result, plan, brief = _full_pipeline(store, now=NOW)
        graph = build_provenance_graph(
            monitor=monitor, verification=verification,
            solver_result=solver_result, plan=plan, brief=brief, now=NOW,
        )
        assert graph.tool_audit is None


# =========================================================================
# Partial pipeline
# =========================================================================


class TestPartialPipeline:
    """build_tool_audit handles missing stages gracefully."""

    def test_monitor_only(self, store):
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        assert audit.total_calls_made == monitor.polls_made
        assert len(audit.module_summaries) == 1
        assert audit.module_summaries[0].module == "monitor"

    def test_empty_produces_zero_counts(self):
        audit = build_tool_audit(now=NOW)
        assert audit.total_calls_made == 0
        assert audit.total_calls_available == 0
        assert audit.total_calls_avoided == 0
        assert len(audit.preconditions) == 0
        assert len(audit.module_summaries) == 0


# =========================================================================
# Real dataset — PO-7712 scenario
# =========================================================================


class TestRealDataset:
    """Run the full pipeline on build_store() and verify the audit reads the
    real preconditions — PO-7712 polled because it's load-bearing."""

    def test_po_7712_is_polled_as_load_bearing(self, store):
        coverage = compute_coverage(store, now=NOW)
        monitor = run_monitor_cycle(store, coverage=coverage, now=NOW)
        audit = build_tool_audit(monitor=monitor, now=NOW)

        po_7712 = [
            p for p in audit.preconditions
            if p.module == "monitor" and p.subject == "PO-7712"
        ]
        assert len(po_7712) == 1
        assert po_7712[0].called is True
        assert "load_bearing" in po_7712[0].precondition
        # The detail should mention it's load-bearing for some production order
        assert "PROD-" in po_7712[0].detail
