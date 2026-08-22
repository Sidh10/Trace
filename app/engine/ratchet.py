"""RATCHET — ARCHITECTURE.md §4 item 6, §3's "RATCHET" stage. Serves Cost
Control (20%) and, structurally, everything upstream: this is the only
component that decides whether a plan actually gets executed.

Deterministic decision logic; the LLM (when enabled) only rephrases the
ALREADY-FINISHED brief into prose — never decides anything (AGENTS.md
rule 1). This module is PLANNER's (item 5) direct consumer: it reads a
`Plan` (or `None`) and the `SolverResult` behind it, and decides
execute-or-escalate. It never calls `POST /erp/update` itself — see
"Scope boundary" below.

--------------------------------------------------------------------------
Three hard triggers — AGENTS.md rule 3, verbatim, all three, non-overridable
--------------------------------------------------------------------------
"Cost above threshold, no supplier meets deadline, or quality risk →
escalate. No confidence score overrides this and the LLM cannot argue past
it." Implemented as three independent boolean checks, each reading only
spec fields or values PLANNER/SOLVER already computed — nothing invented,
nothing blended into a score:

1. **`cost_above_threshold`** — `store.check_approval` (§5.8, the same tool
   `solver.py`'s hard filter already calls) run once against
   `plan.total_cost`. Reused, not re-derived: this module does not
   re-implement per-PO-vs-global threshold selection.
2. **`no_feasible_deadline_plan`** — `plan is None` (nothing survived
   HARD FILTER + Pareto at all) OR `plan.deadline_feasible is False`
   (PLANNER's own deadline hard-filter — item 5's correction [3] — found
   every Pareto candidate missing a high-priority deadline and had to fall
   back to the best infeasible one). Read directly off `Plan`, not
   recomputed: PLANNER already ran `allocate_stock` against every
   candidate to determine this.
3. **`quality_risk`** — `plan is None` AND at least one of `solver_result.
   rejected` carries `reason == "quality_below_threshold"`. Because
   `hard_filter` (item 4, corrected per this session's [1]) already screens
   quality BEFORE Pareto, a *surviving* Pareto candidate can never itself
   fail a component's quality threshold — the only way "the best feasible
   option fails quality" can be TRUE is when quality rejections wiped out
   the candidate pool entirely, leaving no plan. This is a factual read of
   `SolverResult.rejected`, not a new judgment call.

Any one trigger firing forces `decision = "escalate"`. Multiple triggers can
fire together (e.g. a plan can be both over-threshold AND deadline-
infeasible) — the brief lists every trigger that fired, not just the first.
**No trigger fires by default.** A plan that is within budget, deadline-
feasible, and drawn from quality-screened candidates decides `"execute"` —
over-escalation is scored as a failure exactly like under-escalation
(PROJECT.md §2), and this module is tested for both directions.

--------------------------------------------------------------------------
Scope boundary — no ERP write here
--------------------------------------------------------------------------
ARCHITECTURE.md §3's diagram draws RATCHET and "ERP WRITE — the one
irreversible action" as two SEPARATE stages. This module decides and
narrates; it does not call `POST /erp/update` for either outcome (execute
OR escalate — `record_escalation` is one of the six §5.9 ERP actions, and it
would be reasonable for a *later* orchestration layer to call it, but that
layer does not exist yet and this module does not reach for it
unilaterally). Confirmed by a test that the erp_log is untouched by a full
`run_ratchet` call in either direction. Flagged in OPEN_ITEMS.md as a real
scope boundary for whoever builds the orchestrator.

--------------------------------------------------------------------------
The decision brief — every field, and where it comes from
--------------------------------------------------------------------------
  - `chosen plan` — the `Plan` object itself (or `None`), unmodified.
  - `cost delta vs baseline` — `plan.total_cost - plan.cost_of_inaction.
    baseline_total_cost`, both already computed by PLANNER (item 5's
    correction [2]); this module reads, never recomputes.
  - `rejected alternatives with honest signed regret` —
    `plan.rejected_alternatives`, PLANNER's own signed `saved` figures
    (positive = rejecting it saved money; negative = a real regret),
    carried forward verbatim.
  - `cost_of_inaction` — `plan.cost_of_inaction`, the structured,
    non-monetary object from item 5's correction [2]. Never re-derived.
  - `falsification_line` — "what would have to be true for this decision to
    be wrong," built from the SPECIFIC trigger(s) that fired (for escalate)
    or the SPECIFIC assumptions the chosen combination rests on (for
    execute) — see `_falsification_line`. Never a generic sentence; two
    different decisions produce two different lines, tested directly.

--------------------------------------------------------------------------
Narration — LLM-optional, decision-blind
--------------------------------------------------------------------------
`_narrate_deterministic` builds a complete, readable brief from the
already-computed fields — the REQUIRED path (AGENTS.md rule 2), and the
only one exercised when `TRACE_LLM_ENABLED` is false. When the flag is true,
`app.llm.gemini_client.narrate_decision` is given that SAME deterministic
text and asked only to rephrase it; any failure there falls through to the
deterministic text, matching `verify.py`'s established LLM-optional pattern.
Structurally decision-blind: narration runs strictly AFTER `decision` and
every trigger is finalized, and nothing narration returns is ever parsed
back into a decision — proven directly by a test that mocks the LLM call to
return a string arguing for the opposite decision and confirms
`brief.decision` is unaffected.

--------------------------------------------------------------------------
Tool-call count vs necessity
--------------------------------------------------------------------------
`approval_checks_made` is 0 or 1 — one deterministic call to
`store.check_approval` when (and only when) a plan exists to check the cost
of; skipped entirely when `plan is None`, since there is no cost to check.
No RFQ, verification, or solver call happens in this module at all — it
only reads what PLANNER/SOLVER already computed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

from app.config import TRACE_APPROVAL_THRESHOLD
from app.engine.planner import CostOfInaction, Plan, RejectedAlternative
from app.engine.solver import SolverResult
from app.engine.verify import DETERMINISTIC_MODEL_VERSION
from app.environment.clock import clock
from app.environment.schemas import ApprovalCheckRequest
from app.environment.seed_data import STATE, Store

EscalationTrigger = Literal["cost_above_threshold", "no_feasible_deadline_plan", "quality_risk"]


# ==========================================================================
# Output shape
# ==========================================================================


class DecisionBrief(BaseModel):
    """RATCHET's full output. `decision` and `triggers_fired` are the
    load-bearing fields — everything else is context for a human (or an
    audit trail, item 7) to verify the decision against."""

    plan_id: Optional[str]
    component_id: str
    computed_at: datetime

    decision: Literal["execute", "escalate"]
    triggers_fired: list[EscalationTrigger]

    approval_threshold: float
    total_cost: Optional[float]
    cost_delta_vs_baseline: Optional[float]
    cost_increase_vs_baseline_pct: Optional[float]

    chosen_plan: Optional[Plan]
    rejected_alternatives: list[RejectedAlternative]
    cost_of_inaction: Optional[CostOfInaction]
    falsification_line: str

    narration: str
    narrated_by: Literal["llm", "deterministic"]
    model_version: str

    approval_checks_made: int

    def executed(self) -> bool:
        return self.decision == "execute"


# ==========================================================================
# The three triggers
# ==========================================================================


def _evaluate_triggers(
    store: Store,
    plan: Optional[Plan],
    solver_result: SolverResult,
) -> tuple[list[EscalationTrigger], int]:
    triggers: list[EscalationTrigger] = []
    approval_checks_made = 0

    if plan is not None:
        approval = store.check_approval(
            ApprovalCheckRequest(action="ratchet_execute_plan", estimated_cost=plan.total_cost)
        )
        approval_checks_made += 1
        if approval.approval_required:
            triggers.append("cost_above_threshold")

    if plan is None or not plan.deadline_feasible:
        triggers.append("no_feasible_deadline_plan")

    if plan is None and any(r.reason == "quality_below_threshold" for r in solver_result.rejected):
        triggers.append("quality_risk")

    return triggers, approval_checks_made


# ==========================================================================
# Falsification line — computed per decision, not boilerplate
# ==========================================================================


def _falsification_line(
    decision: Literal["execute", "escalate"],
    triggers: list[EscalationTrigger],
    plan: Optional[Plan],
    threshold: float,
) -> str:
    if decision == "execute":
        assert plan is not None  # execute is unreachable without a plan
        chosen = plan.chosen_combination
        supplier_list = ", ".join(chosen.supplier_ids)
        return (
            f"This executes because total_cost ({plan.total_cost:.2f}) is within "
            f"the {threshold:.0f} approval threshold, every high-priority production "
            f"order reaches its own deadline under this plan, and {supplier_list} "
            "passed the certification and quality-score hard filter before ever "
            "being quoted. It would be wrong to execute if "
            f"{chosen.supplier_ids[0]}'s reliability_score "
            f"({chosen.reliability_score}) does not hold in practice — e.g. a "
            "future verification cycle finds one of its claims contradicted — or "
            f"if {threshold:.0f} is not actually the correct approval threshold for "
            "this specific purchase."
        )

    clauses: list[str] = []
    reversals: list[str] = []

    if "cost_above_threshold" in triggers and plan is not None:
        clauses.append(
            f"total_cost ({plan.total_cost:.2f}) exceeds the {threshold:.0f} approval "
            "threshold"
        )
        reversals.append(f"the {threshold:.0f} threshold used here is not the correct one")

    if "no_feasible_deadline_plan" in triggers:
        if plan is not None:
            clauses.append(
                "no Pareto-feasible sourcing combination meets every high-priority "
                "production deadline — the best available option still misses one"
            )
        else:
            clauses.append(
                "no sourcing combination survived the hard filter and Pareto pass "
                "at all"
            )
        reversals.append(
            "a faster or better-supplied option exists that this pass did not "
            "correctly capture"
        )

    if "quality_risk" in triggers:
        clauses.append(
            "every remaining candidate was rejected on quality_score below the "
            "component's required threshold"
        )
        reversals.append("a supplier's quality_score was recorded incorrectly")

    reason_text = "; ".join(clauses)
    reversal_text = ", or ".join(reversals) if reversals else "the underlying data has changed"
    return (
        f"This escalates because {reason_text}. It would be wrong to escalate if "
        f"{reversal_text}, or if the underlying deadline/threshold/quality data has "
        "changed since this was computed."
    )


# ==========================================================================
# Narration — deterministic required path, LLM bolted on top
# ==========================================================================


def _narrate_deterministic(brief_facts: "_BriefFacts") -> str:
    """The REQUIRED path (AGENTS.md rule 2) — a complete, readable brief
    built entirely from already-computed fields. Not a stub: every line
    below cites a real field, so two different decisions render two
    different briefs, not a fixed template with blanks filled in."""
    lines = [
        f"DECISION: {brief_facts.decision.upper()} — {brief_facts.component_id}",
        "",
    ]
    if brief_facts.triggers:
        lines.append("Triggers fired: " + ", ".join(brief_facts.triggers))
    else:
        lines.append("Triggers fired: none")
    lines.append("")

    if brief_facts.plan is not None:
        chosen = brief_facts.plan.chosen_combination
        lines.append(
            f"Chosen plan {brief_facts.plan.plan_id}: {chosen.label}, "
            f"total_cost {brief_facts.plan.total_cost:.2f} "
            f"(approval threshold {brief_facts.threshold:.0f})."
        )
        if brief_facts.cost_delta is not None:
            lines.append(
                f"Cost delta vs baseline: {brief_facts.cost_delta:+.2f} "
                f"({brief_facts.cost_pct:+.2f}%)."
                if brief_facts.cost_pct is not None
                else f"Cost delta vs baseline: {brief_facts.cost_delta:+.2f}."
            )
        else:
            lines.append("Cost delta vs baseline: not available (no disrupted PO identified).")

        if brief_facts.cost_of_inaction and brief_facts.cost_of_inaction.production_orders_at_risk:
            lines.append("Production orders at risk if this plan is not executed:")
            for order in brief_facts.cost_of_inaction.production_orders_at_risk:
                delay = (
                    f"{order.deadline_missed_by_days}d late"
                    if order.deadline_missed_by_days is not None
                    else "never fully supplied by this plan"
                )
                lines.append(
                    f"  - {order.production_order_id} ({order.priority} priority): "
                    f"{order.units_unbuilt} units unbuilt, {delay}."
                )
        else:
            lines.append("Production orders at risk: none.")

        if brief_facts.rejected:
            lines.append("Rejected alternatives:")
            for alt in brief_facts.rejected[:5]:
                saved = f"{alt.saved:+.2f}" if alt.saved is not None else "n/a"
                lines.append(f"  - {alt.option} [{alt.reason}]: saved {saved} — {alt.regret}")
    else:
        lines.append("No feasible plan was produced by the solver pass at all.")

    lines.append("")
    lines.append(f"Falsification: {brief_facts.falsification}")
    return "\n".join(lines)


class _BriefFacts(BaseModel):
    """Internal carrier for the fields `_narrate_deterministic` renders —
    exists so the narration function takes one argument instead of eight,
    not a public output shape."""

    component_id: str
    decision: str
    triggers: list[str]
    plan: Optional[Plan]
    threshold: float
    cost_delta: Optional[float]
    cost_pct: Optional[float]
    cost_of_inaction: Optional[CostOfInaction]
    rejected: list[RejectedAlternative]
    falsification: str


def _narrate(facts: "_BriefFacts") -> tuple[str, Literal["llm", "deterministic"], str]:
    """Returns (text, narrated_by, model_version). Decision-blind by
    construction: `facts.decision` and `facts.triggers` are already final
    by the time this runs, and this function's return value is never read
    by anything except the `DecisionBrief.narration` display field."""
    from app.config import TRACE_LLM_ENABLED

    deterministic_text = _narrate_deterministic(facts)

    if TRACE_LLM_ENABLED:
        try:
            from app.llm import gemini_client

            llm_text = gemini_client.narrate_decision(deterministic_text)
            return llm_text, "llm", gemini_client.MODEL_VERSION
        except Exception:
            # AGENTS.md rule 2 — the LLM is never load-bearing. Fall through
            # to the required deterministic path below.
            pass

    return deterministic_text, "deterministic", DETERMINISTIC_MODEL_VERSION


# ==========================================================================
# The pass
# ==========================================================================


def run_ratchet(
    store: Optional[Store],
    plan: Optional[Plan],
    solver_result: SolverResult,
    *,
    now: Optional[datetime] = None,
) -> DecisionBrief:
    """HARD FILTER, SOLVER (item 4) and PLAN (item 5) already ran; this is
    RATCHET (item 6) — the last deterministic decision before an ERP write
    that this module does not itself make (see "Scope boundary").

    `plan` may be `None` (nothing survived hard filter + Pareto at all,
    per item 5's own contract) — `run_ratchet` handles that case as a
    guaranteed escalation, not a crash.
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now

    triggers, approval_checks_made = _evaluate_triggers(store, plan, solver_result)
    decision: Literal["execute", "escalate"] = "escalate" if triggers else "execute"

    threshold = TRACE_APPROVAL_THRESHOLD
    falsification = _falsification_line(decision, triggers, plan, threshold)

    cost_delta = None
    cost_pct = None
    rejected: list[RejectedAlternative] = []
    cost_of_inaction: Optional[CostOfInaction] = None
    if plan is not None:
        rejected = plan.rejected_alternatives
        cost_of_inaction = plan.cost_of_inaction
        if cost_of_inaction.baseline_total_cost is not None:
            cost_delta = round(plan.total_cost - cost_of_inaction.baseline_total_cost, 2)
        cost_pct = cost_of_inaction.cost_increase_vs_baseline_pct

    facts = _BriefFacts(
        component_id=plan.component_id if plan is not None else solver_result.component_id,
        decision=decision,
        triggers=list(triggers),
        plan=plan,
        threshold=threshold,
        cost_delta=cost_delta,
        cost_pct=cost_pct,
        cost_of_inaction=cost_of_inaction,
        rejected=rejected,
        falsification=falsification,
    )
    narration, narrated_by, model_version = _narrate(facts)

    return DecisionBrief(
        plan_id=plan.plan_id if plan is not None else None,
        component_id=facts.component_id,
        computed_at=now,
        decision=decision,
        triggers_fired=triggers,
        approval_threshold=threshold,
        total_cost=plan.total_cost if plan is not None else None,
        cost_delta_vs_baseline=cost_delta,
        cost_increase_vs_baseline_pct=cost_pct,
        chosen_plan=plan,
        rejected_alternatives=rejected,
        cost_of_inaction=cost_of_inaction,
        falsification_line=falsification,
        narration=narration,
        narrated_by=narrated_by,
        model_version=model_version,
        approval_checks_made=approval_checks_made,
    )
