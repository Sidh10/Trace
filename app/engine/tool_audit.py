"""TOOL AUDIT — ARCHITECTURE.md §4 item 10, the Tool Efficiency (10%) rubric row.

Deterministic Python. No LLM, no mutations, no tool calls of its own — this
module only READS the per-call precondition records that upstream modules
already populate (AGENTS.md rule 1, and OPEN_ITEMS.md's own guidance: "item
10 should read them rather than adding a second logging path").

What it produces:
  * A per-call precondition log — every tool call the pipeline made or
    deliberately skipped, with the structured reason from the upstream
    decision record that owned it. One entry per decision, not one per module.
  * A count-vs-necessity summary — the raw numbers an auditor or judge needs
    to verify that calls were justified, broken down per module, with the
    gating rule that governed each.

What it does NOT produce:
  * No efficiency ratio, score, or percentage. The summary carries counts
    and plain-language reasons; the reader does whatever arithmetic they want.
    AGENTS.md rule 7 forbids inventing a metric and displaying it as a
    finding, and "calls_made / calls_available = 30% efficiency" would be
    exactly that — a ratio whose denominator is arbitrary (why is "available"
    the right baseline?) dressed up as a score. A test enforces this.

Where the per-call preconditions come from (OPEN_ITEMS.md, verbatim):
  - PollDecision.reason ("load_bearing" / "not_load_bearing") — MONITOR
  - VerificationSkip.reason ("no_supplier_claim_on_record" /
    "no_tracking_record") — VERIFY
  - ClaimVerification.probe_source ("reused_from_monitor" / "own_probe")
    — VERIFY
  - SolverResult.quotes_requested / quotes_reused — SOLVER
  - Rejection.reason (hard-filtered suppliers, never quoted) — SOLVER
  - DecisionBrief.approval_checks_made (0 or 1) — RATCHET
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.engine.monitor import MonitorReport
from app.engine.ratchet import DecisionBrief
from app.engine.solver import SolverResult
from app.engine.verify import VerificationReport
from app.environment.clock import clock


# ==========================================================================
# Output shapes
# ==========================================================================

Module = Literal["monitor", "verify", "solver", "ratchet"]


class ToolCallPrecondition(BaseModel):
    """One call-or-skip decision, with the structured reason from the upstream
    record that owned it.

    `called` is True when a tool call was actually made (a tracking poll, a
    verification probe, an RFQ request, an approval check). False when the
    call was deliberately skipped or reused from a prior stage.

    `precondition` is the upstream record's own reason field, verbatim — not
    a restatement or a judgement by this module.
    """

    module: Module
    subject: str              # what the call was about: po_id, supplier_id, plan_id
    called: bool
    precondition: str         # the upstream reason, verbatim
    detail: str = ""          # extra context (e.g. "load-bearing for PROD-882, PROD-914")


class ModuleCallSummary(BaseModel):
    """Per-module breakdown of calls made vs available."""

    module: Module
    calls_made: int
    calls_available: int
    calls_avoided: int
    gating_rule: str          # plain-language description of the gate


class ToolAuditReport(BaseModel):
    """Item 10's full output: per-call preconditions + count-vs-necessity
    summary.

    Sits alongside `ToolCallSummary` (item 7's counters) on the
    `ProvenanceGraph`, never replacing it. Item 7 owns the counters; this
    module owns the per-call breakdown and the plain-language summary.
    """

    computed_at: datetime
    preconditions: list[ToolCallPrecondition]
    module_summaries: list[ModuleCallSummary]
    total_calls_made: int
    total_calls_available: int
    total_calls_avoided: int
    necessity_verdict: str    # one plain-language sentence


# ==========================================================================
# Builder
# ==========================================================================


def _audit_monitor(monitor: MonitorReport) -> list[ToolCallPrecondition]:
    """One precondition per PollDecision — polled and skipped alike."""
    preconditions: list[ToolCallPrecondition] = []
    for decision in monitor.decisions:
        detail = ""
        if decision.load_bearing_for:
            detail = (
                f"load-bearing for {', '.join(decision.load_bearing_for)}"
                f" ({decision.exposure_days} days of coverage at risk)"
            )
        preconditions.append(
            ToolCallPrecondition(
                module="monitor",
                subject=decision.po_id,
                called=decision.polled,
                precondition=decision.reason,
                detail=detail,
            )
        )
    return preconditions


def _monitor_summary(monitor: MonitorReport) -> ModuleCallSummary:
    avoided = monitor.polls_available - monitor.polls_made
    return ModuleCallSummary(
        module="monitor",
        calls_made=monitor.polls_made,
        calls_available=monitor.polls_available,
        calls_avoided=avoided,
        gating_rule=(
            "Tracking polled only for load-bearing POs — those whose withdrawal "
            "alone would turn some production order critical (ARCHITECTURE.md §4 "
            "item 2b). Non-load-bearing POs skipped."
        ),
    )


def _audit_verify(verification: VerificationReport) -> list[ToolCallPrecondition]:
    """One precondition per ClaimVerification + one per VerificationSkip."""
    preconditions: list[ToolCallPrecondition] = []
    for check in verification.verifications:
        called = check.probe_source == "own_probe"
        precondition = (
            f"probe_source={check.probe_source}"
            + (f", contradicted={check.contradicted}" if called else "")
        )
        detail = (
            f"claim '{check.claim.claim_status}' vs tracking "
            f"'{check.tracking_status}'"
        )
        preconditions.append(
            ToolCallPrecondition(
                module="verify",
                subject=check.po_id,
                called=called,
                precondition=precondition,
                detail=detail,
            )
        )
    for skip in verification.skipped:
        preconditions.append(
            ToolCallPrecondition(
                module="verify",
                subject=skip.po_id,
                called=False,
                precondition=f"skip_reason={skip.reason}",
                detail=f"No verification needed: {skip.reason}",
            )
        )
    return preconditions


def _verify_summary(verification: VerificationReport) -> ModuleCallSummary:
    total_available = verification.probes_made + verification.probes_reused_from_monitor
    return ModuleCallSummary(
        module="verify",
        calls_made=verification.probes_made,
        calls_available=total_available,
        calls_avoided=verification.probes_reused_from_monitor,
        gating_rule=(
            "Tracking probes reused from MONITOR's read of the same PO in the "
            "same cycle. New probes issued only for POs MONITOR did not already poll."
        ),
    )


def _audit_solver(solver_result: SolverResult) -> list[ToolCallPrecondition]:
    """One precondition per hard-filter rejection (supplier never quoted)."""
    preconditions: list[ToolCallPrecondition] = []
    # Hard-filter rejections: these suppliers were dropped BEFORE spending an
    # RFQ call (ARCHITECTURE.md §3's "HARD FILTER — drop uncertified /
    # budget-infeasible candidates BEFORE spending an RFQ call").
    hard_filter_reasons = frozenset({
        "uncertified", "quality_below_threshold", "budget_infeasible",
    })
    for rejection in solver_result.rejected:
        if rejection.reason in hard_filter_reasons:
            preconditions.append(
                ToolCallPrecondition(
                    module="solver",
                    subject=rejection.subject,
                    called=False,
                    precondition=f"hard_filter_drop={rejection.reason}",
                    detail=rejection.note,
                )
            )
    return preconditions


def _solver_summary(solver_result: SolverResult) -> ModuleCallSummary:
    # Hard-filter drops are suppliers that were available but never quoted.
    hard_filter_reasons = frozenset({
        "uncertified", "quality_below_threshold", "budget_infeasible",
    })
    hard_filtered = sum(
        1 for r in solver_result.rejected
        if r.reason in hard_filter_reasons
    )
    available = solver_result.quotes_requested + solver_result.quotes_reused + hard_filtered
    return ModuleCallSummary(
        module="solver",
        calls_made=solver_result.quotes_requested,
        calls_available=available,
        calls_avoided=solver_result.quotes_reused + hard_filtered,
        gating_rule=(
            "RFQ calls issued only for suppliers surviving the hard filter "
            "(certification + quality + budget). Hard-filtered suppliers never "
            "quoted. Still-valid quotes from a prior cycle reused."
        ),
    )


def _audit_ratchet(brief: DecisionBrief) -> list[ToolCallPrecondition]:
    """Zero or one precondition — the approval check, when it happened."""
    if brief.approval_checks_made == 0:
        return [
            ToolCallPrecondition(
                module="ratchet",
                subject=brief.component_id,
                called=False,
                precondition="no_plan_to_check",
                detail="No plan exists to check the cost of; approval check skipped.",
            )
        ]
    return [
        ToolCallPrecondition(
            module="ratchet",
            subject=brief.plan_id or brief.component_id,
            called=True,
            precondition="plan_cost_requires_approval_check",
            detail=(
                f"Checked plan total_cost={brief.total_cost} against "
                f"threshold={brief.approval_threshold}; "
                f"decision='{brief.decision}'."
            ),
        )
    ]


def _ratchet_summary(brief: DecisionBrief) -> ModuleCallSummary:
    # Approval check is 0 or 1; available is always 1 when there's a plan.
    available = 1 if brief.plan_id is not None else 0
    return ModuleCallSummary(
        module="ratchet",
        calls_made=brief.approval_checks_made,
        calls_available=available,
        calls_avoided=max(0, available - brief.approval_checks_made),
        gating_rule=(
            "Approval check issued once per plan. Skipped entirely when no "
            "plan exists (nothing to check the cost of)."
        ),
    )


def _build_verdict(summaries: list[ModuleCallSummary]) -> str:
    """One plain-language sentence summarising the tool discipline."""
    total_made = sum(s.calls_made for s in summaries)
    total_avoided = sum(s.calls_avoided for s in summaries)
    parts: list[str] = []
    for s in summaries:
        if s.calls_made > 0 or s.calls_avoided > 0:
            mod = s.module.upper()
            parts.append(f"{mod}: {s.calls_made} made, {s.calls_avoided} avoided")
    breakdown = "; ".join(parts)
    return (
        f"{total_made} tool call(s) made, {total_avoided} avoided by gating "
        f"or reuse. {breakdown}."
    )


def build_tool_audit(
    *,
    monitor: Optional[MonitorReport] = None,
    verification: Optional[VerificationReport] = None,
    solver_result: Optional[SolverResult] = None,
    brief: Optional[DecisionBrief] = None,
    now: Optional[datetime] = None,
) -> ToolAuditReport:
    """Build the tool audit report from whichever stages actually ran.

    Every argument is optional so a partial pipeline produces a partial but
    still valid report, matching `build_provenance_graph`'s convention.
    """
    now = clock.now() if now is None else now
    preconditions: list[ToolCallPrecondition] = []
    summaries: list[ModuleCallSummary] = []

    if monitor is not None:
        preconditions.extend(_audit_monitor(monitor))
        summaries.append(_monitor_summary(monitor))
    if verification is not None:
        preconditions.extend(_audit_verify(verification))
        summaries.append(_verify_summary(verification))
    if solver_result is not None:
        preconditions.extend(_audit_solver(solver_result))
        summaries.append(_solver_summary(solver_result))
    if brief is not None:
        preconditions.extend(_audit_ratchet(brief))
        summaries.append(_ratchet_summary(brief))

    total_made = sum(s.calls_made for s in summaries)
    total_available = sum(s.calls_available for s in summaries)
    total_avoided = sum(s.calls_avoided for s in summaries)

    return ToolAuditReport(
        computed_at=now,
        preconditions=preconditions,
        module_summaries=summaries,
        total_calls_made=total_made,
        total_calls_available=total_available,
        total_calls_avoided=total_avoided,
        necessity_verdict=_build_verdict(summaries),
    )
