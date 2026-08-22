"""PROVENANCE GRAPH — ARCHITECTURE.md §4 item 7, §3's "AUDIT" stage. Serves
Audit & Explainability (10%) and Cost Control's justification half (20%).

ONE object, not two. ARCHITECTURE.md §4 item 7 specifies "audit trail and
assumption ledger as ONE object" — `ProvenanceGraph` holds the typed edge
trail (`edges`), the assumption ledger (`assumptions`), the per-decision
reproducibility log (`decisions`), the regret ledger (`regret_ledger` /
`cost_of_inaction`), and the tool-call accounting (`tool_calls`) in a single
record. Splitting these into separate objects is exactly what item 7 exists
to prevent: an audit trail that doesn't carry the assumptions it rests on
can't be checked, and an assumption ledger with no trail can't be traced.

Deterministic construction, no LLM anywhere in this module (AGENTS.md
rule 1). The graph records what the engine already decided; it never decides
anything itself and never recomputes an upstream number. Narration of a
finished graph may use item 6's existing LLM-optional pattern — but that is
`ratchet.py`'s function, not this module's, and nothing here calls it.

--------------------------------------------------------------------------
Six typed relations, and nothing else
--------------------------------------------------------------------------
`Support | Depend-on | Contradict | Invalidate | Trigger | Update`, exactly
ARCHITECTURE.md §7's set. Direction reads "FROM <relation> TO", matching
§7's own worked example (`from: tracking:PO-7712`, `to:
claim:SUP-21-dispatched`, `relation: Contradict` = "tracking contradicts the
claim").

**An edge exists or it doesn't.** There is no severity, weight, confidence,
or score field on `ProvenanceEdge` and there must never be one — AGENTS.md
rule 7 forbids inventing a metric and displaying it as a finding, and a
"0.7-strength Support edge" would be precisely that. What an edge carries
instead is *where it came from*: `produced_by_module`, `input_record_ids`
(the actual record IDs consumed), and `model_version`.

--------------------------------------------------------------------------
model_version — the real field, never invented, and never overclaimed
--------------------------------------------------------------------------
Every edge carries `model_version` in ARCHITECTURE.md §7's own format
(`"deterministic"` or `"gemini-<version>"`). Where it comes from, per edge
source:
  - MONITOR / COVERAGE / SOLVER / PLANNER edges — `"deterministic"`
    (`verify.DETERMINISTIC_MODEL_VERSION`, imported not hardcoded). None of
    those modules can reach an LLM.
  - VERIFY edges — `ClaimVerification.claim.model_version`, item 3's real
    field, read off the record. This is the one that can legitimately read
    `"gemini-3.6-flash"`, and the one that silently flips to
    `"deterministic"` if the LLM call failed mid-run — see verify.py's own
    `ParsedClaim` docstring.
  - RATCHET edges — `"deterministic"`, NOT `DecisionBrief.model_version`.
    This distinction is deliberate and load-bearing: `DecisionBrief.
    model_version` describes the *narration* (which may be LLM-rephrased),
    while the decision itself comes from `_evaluate_triggers`, which is pure
    Python and cannot be reached by an LLM at all (AGENTS.md rules 1 and 3).
    Tagging a `Trigger` edge with the narration's model version would assert
    in the audit trail that an LLM produced the escalation decision — the
    exact claim rules 1 and 3 exist to make false. The narration's own model
    version is recorded separately on `DecisionRecord.narration_model_version`,
    where it belongs.

--------------------------------------------------------------------------
What is wired, and to which real object
--------------------------------------------------------------------------
Built against the shipped field names in `coverage.py` / `monitor.py` /
`verify.py` / `solver.py` / `planner.py` / `ratchet.py` — not against
ARCHITECTURE.md §7's pre-item-6 placeholder examples.

  * **item 2 (coverage)** — `Depend-on` per `CoverageResult.
    depends_on_po_ids` (that field is literally the dependency, read not
    re-derived); `Contradict` per `InventoryContradiction`, honoring the
    mapping `coverage.py`'s own docstring already promised
    (`from="warehouse:{component_id}"`, `to="erp:{component_id}"`).
  * **item 2b (monitor)** — `Support` from the poll to the `DisruptionEvent`
    it detected. Polls that ran and found nothing get an explicit `Update`
    edge instead, applying the same anti-ambiguity principle item 6's
    "no trigger" edge uses: silence must never be ambiguous between
    "checked, consistent" and "never checked".
  * **item 3 (verify)** — `Contradict` from `tracking:{po_id}` to
    `claim:{message_id}`, then `Invalidate` from that same tracking record
    onto `reliability:{supplier_id}`. The `from` node is the TRACKING record
    on both, which is AGENTS.md rule 4 made structural: the score moved
    because tracking contradicted the claim, and the graph says so — never
    because an email read a certain way.
  * **item 4 (solver)** — `Support` per `Rejection`, citing the exact
    `DropReason` (`quality_below_threshold`, `budget_infeasible`,
    `uncertified`, `expired_quote`, `dominated`) and the rejection's own
    `note` verbatim.
  * **item 5 (planner)** — `Invalidate` onto every combination the
    deadline-feasibility hard filter dropped, citing the `RejectedAlternative`
    whose `reason == "deadline_infeasible"` and its `regret` verbatim (that
    text names the specific order and the specific day numbers — see the
    "Regret" section below for why it is cited rather than re-parsed).
  * **item 6 (ratchet)** — `Trigger` from each fired condition to the
    `DecisionBrief`; when none fired, one explicit `Update` edge so silence
    is unambiguous.

--------------------------------------------------------------------------
Regret — the same objects, not copies of their numbers
--------------------------------------------------------------------------
`regret_ledger` and `cost_of_inaction` hold PLANNER's own
`RejectedAlternative` / `CostOfInaction` instances, not restatements of
their figures. Pydantic preserves instance identity for already-validated
models, so `graph.regret_ledger[i] is plan.rejected_alternatives[i]` holds —
verified by a test. Nothing in this module reads `saved`,
`cost_increase_vs_baseline_pct`, or `units_unbuilt` and writes a second copy
somewhere; a discrepancy between the graph's regret figures and the plan's
is therefore structurally impossible rather than merely unlikely. Edges
reference these entries by their `option` string, which is the ledger's own
key.

--------------------------------------------------------------------------
Tool-call count vs necessity (item 10 consumes this)
--------------------------------------------------------------------------
`ToolCallSummary` totals only counters the upstream reports already
maintain — `MonitorReport.polls_made/polls_available`,
`VerificationReport.probes_made/probes_reused_from_monitor`,
`SolverResult.quotes_requested/quotes_reused`,
`DecisionBrief.approval_checks_made`. `calls_avoided_by_gating` is the sum
of calls the gates demonstrably prevented (polls skipped as not-load-bearing,
probes reused from MONITOR's read, quotes reused within validity). No ratio
or efficiency score is computed here: that is item 10's call to make, and
inventing one now would be a metric this module has no mandate for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.engine.coverage import CoverageReport
from app.engine.monitor import MonitorReport
from app.engine.planner import CostOfInaction, Plan, RejectedAlternative
from app.engine.ratchet import DecisionBrief
from app.engine.solver import SolverResult
from app.engine.verify import DETERMINISTIC_MODEL_VERSION, VerificationReport
from app.environment.clock import clock
from app.environment.schemas import ERPUpdateResponse

# ARCHITECTURE.md §7's exact relation set. Six, closed, no additions.
Relation = Literal["Support", "Depend-on", "Contradict", "Invalidate", "Trigger", "Update"]

# Which pipeline module emitted an edge. Matches the module filenames so an
# auditor can go straight to the source.
ProducingModule = Literal["coverage", "monitor", "verify", "solver", "planner", "ratchet"]

# Every node id is "<kind>:<identifier>" (§7's own convention, e.g.
# "tracking:PO-7712"). Closed set so a typo becomes a validation failure
# rather than a silently orphaned node — see `unknown_node_kinds()`.
NODE_KINDS: frozenset[str] = frozenset(
    {
        "poll",              # a MONITOR PollDecision, keyed by po_id
        "event",             # a DisruptionEvent, keyed by event_id
        "tracking",          # a tracking record, keyed by po_id
        "claim",             # a ParsedClaim, keyed by message_id
        "reliability",       # a supplier's reliability score, keyed by supplier_id
        "supplier",          # a SupplierRecord, keyed by supplier_id
        "rejection",         # a solver Rejection, keyed by its subject
        "combination",       # a SourcingCombination, keyed by its label
        "plan",              # a Plan, keyed by plan_id
        "brief",             # a DecisionBrief, keyed by plan_id or component_id
        "trigger",           # a fired EscalationTrigger, keyed by its name
        "ratchet_check",     # the ratchet's own no-trigger check, keyed by component_id
        "production_order",  # a ProductionOrder, keyed by production_order_id
        "po",                # a PurchaseOrder, keyed by po_id
        "warehouse",         # warehouse-truth stock figure, keyed by component_id
        "erp",               # ERP headline stock figure, keyed by component_id
        "deadline_constraint",  # the deadline feasibility filter, keyed by component_id
        "erp_write",         # one POST /erp/update response, keyed by update_id
        "contingency",       # a pre-committed fallback, keyed by contingency_id
    }
)


def node(kind: str, identifier: str) -> str:
    """Build a node id. Kind is validated at graph level, not here, so a
    caller can't be silently wrong without `unknown_node_kinds()` catching
    it."""
    return f"{kind}:{identifier}"


def node_kind(node_id: str) -> str:
    return node_id.split(":", 1)[0]


# ==========================================================================
# Shapes
# ==========================================================================


class ProvenanceEdge(BaseModel):
    """One typed edge. ARCHITECTURE.md §7's shape, with `from`/`to` kept as
    serialization aliases (the repo's established pattern — see
    `schemas.py`'s `SupplierMessage.from_`) because `from` is a Python
    keyword.

    There is deliberately no severity/weight/confidence field — see the
    module docstring. An edge exists or it doesn't."""

    model_config = ConfigDict(populate_by_name=True)

    edge_id: str
    relation: Relation
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")

    produced_by_module: ProducingModule
    # The actual record IDs this edge was derived from — po_id, message_id,
    # supplier_id, production_order_id, plan_id, event_id. What an auditor
    # re-reads to check the edge.
    input_record_ids: list[str] = Field(default_factory=list)
    model_version: str
    timestamp: datetime
    # Deterministic text, quoting the source object where the source object
    # already has prose (Rejection.note, RejectedAlternative.regret,
    # ClaimVerification.contradiction_reason). Never a restatement of a
    # number the source object already holds.
    note: str


class Assumption(BaseModel):
    """One entry in the assumption ledger — the half of item 7 that is NOT
    the edge trail. Each cites the exact object field it was taken from and
    carries that field's text verbatim, so the ledger cannot drift from the
    decision it qualifies."""

    assumption_id: str
    subject_node: str
    statement: str       # verbatim from source_field
    source_module: ProducingModule
    source_field: str    # e.g. "Plan.selection_rule"
    model_version: str
    timestamp: datetime


class DecisionRecord(BaseModel):
    """AGENTS.md rule 7's reproducibility requirement: one model_version-
    tagged entry per decision.

    `model_version` is the version behind the DECISION. `narration_model_version`
    is populated only where a decision also produced human-facing prose whose
    provenance differs — currently only RATCHET, whose decision is always
    deterministic while its narration may be LLM-rephrased. Keeping them in
    two fields is what stops the audit trail from implying an LLM decided
    something it only described."""

    decision_id: str
    decision_node: str
    module: ProducingModule
    outcome: str
    input_record_ids: list[str] = Field(default_factory=list)
    model_version: str
    narration_model_version: Optional[str] = None
    timestamp: datetime


class ToolCallSummary(BaseModel):
    """Counts only — every field is a counter an upstream report already
    maintains. No ratio or efficiency score: item 10 owns that."""

    monitor_polls_made: int = 0
    monitor_polls_available: int = 0
    verify_probes_made: int = 0
    verify_probes_reused_from_monitor: int = 0
    solver_quotes_requested: int = 0
    solver_quotes_reused: int = 0
    ratchet_approval_checks_made: int = 0

    total_calls_made: int = 0
    calls_avoided_by_gating: int = 0
    notes: list[str] = Field(default_factory=list)


class ProvenanceGraph(BaseModel):
    """Item 7's ONE object: trail + ledger + reproducibility log + regret +
    tool accounting."""

    built_at: datetime
    edges: list[ProvenanceEdge]
    assumptions: list[Assumption]
    decisions: list[DecisionRecord]
    # PLANNER's own objects, by reference — see the module docstring's
    # "Regret" section. Never copies of their numbers.
    regret_ledger: list[RejectedAlternative] = Field(default_factory=list)
    cost_of_inaction: Optional[CostOfInaction] = None
    tool_calls: ToolCallSummary

    # -- integrity ---------------------------------------------------------

    def nodes(self) -> list[str]:
        seen: list[str] = []
        for edge in self.edges:
            for endpoint in (edge.from_node, edge.to_node):
                if endpoint not in seen:
                    seen.append(endpoint)
        return seen

    def unknown_node_kinds(self) -> list[str]:
        """Node ids whose kind isn't in NODE_KINDS — a typo'd or invented
        node type, which would otherwise sit in the graph looking real."""
        return [n for n in self.nodes() if node_kind(n) not in NODE_KINDS]

    def duplicate_edges(self) -> list[str]:
        """Edge ids whose (relation, from, to) triple already appeared. The
        same fact asserted twice is a defect: it double-counts evidence."""
        seen: set[tuple[str, str, str]] = set()
        duplicates: list[str] = []
        for edge in self.edges:
            key = (edge.relation, edge.from_node, edge.to_node)
            if key in seen:
                duplicates.append(edge.edge_id)
            else:
                seen.add(key)
        return duplicates

    def edges_from(self, node_id: str) -> list[ProvenanceEdge]:
        return [e for e in self.edges if e.from_node == node_id]

    def edges_to(self, node_id: str) -> list[ProvenanceEdge]:
        return [e for e in self.edges if e.to_node == node_id]

    def trace_path(self, start: str, end: str) -> Optional[list[ProvenanceEdge]]:
        """Breadth-first walk along edge direction (from -> to). Returns the
        edge sequence connecting `start` to `end`, or None if no directed
        path exists. Deterministic: edges are visited in insertion order."""
        if start == end:
            return []
        queue: list[tuple[str, list[ProvenanceEdge]]] = [(start, [])]
        visited: set[str] = {start}
        while queue:
            current, path = queue.pop(0)
            for edge in self.edges_from(current):
                if edge.to_node == end:
                    return [*path, edge]
                if edge.to_node not in visited:
                    visited.add(edge.to_node)
                    queue.append((edge.to_node, [*path, edge]))
        return None


# ==========================================================================
# Id sequences — same pattern as coverage.py's event sequence
# ==========================================================================

_edge_seq = 0
_assumption_seq = 0
_decision_seq = 0


def next_edge_id() -> str:
    global _edge_seq
    _edge_seq += 1
    return f"PROV-{_edge_seq:04d}"


def next_assumption_id() -> str:
    global _assumption_seq
    _assumption_seq += 1
    return f"ASSUM-{_assumption_seq:04d}"


def next_decision_id() -> str:
    global _decision_seq
    _decision_seq += 1
    return f"DEC-{_decision_seq:04d}"


def reset_provenance_sequences() -> None:
    """Test hook, matching `coverage.reset_event_sequence()` /
    `planner.reset_plan_sequence()`."""
    global _edge_seq, _assumption_seq, _decision_seq
    _edge_seq = 0
    _assumption_seq = 0
    _decision_seq = 0


# ==========================================================================
# Builder
# ==========================================================================


class _Builder:
    """Accumulates edges/assumptions/decisions. Exists so each `_add_*`
    stage below reads as a flat list of facts rather than threading three
    lists through six functions."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.edges: list[ProvenanceEdge] = []
        self.assumptions: list[Assumption] = []
        self.decisions: list[DecisionRecord] = []

    def edge(
        self,
        relation: Relation,
        from_node: str,
        to_node: str,
        module: ProducingModule,
        input_record_ids: list[str],
        note: str,
        model_version: str = DETERMINISTIC_MODEL_VERSION,
    ) -> None:
        self.edges.append(
            ProvenanceEdge(
                edge_id=next_edge_id(),
                relation=relation,
                from_node=from_node,
                to_node=to_node,
                produced_by_module=module,
                input_record_ids=input_record_ids,
                model_version=model_version,
                timestamp=self.now,
                note=note,
            )
        )

    def assume(
        self,
        subject_node: str,
        statement: str,
        module: ProducingModule,
        source_field: str,
        model_version: str = DETERMINISTIC_MODEL_VERSION,
    ) -> None:
        self.assumptions.append(
            Assumption(
                assumption_id=next_assumption_id(),
                subject_node=subject_node,
                statement=statement,
                source_module=module,
                source_field=source_field,
                model_version=model_version,
                timestamp=self.now,
            )
        )

    def decide(
        self,
        decision_node: str,
        module: ProducingModule,
        outcome: str,
        input_record_ids: list[str],
        model_version: str = DETERMINISTIC_MODEL_VERSION,
        narration_model_version: Optional[str] = None,
    ) -> None:
        self.decisions.append(
            DecisionRecord(
                decision_id=next_decision_id(),
                decision_node=decision_node,
                module=module,
                outcome=outcome,
                input_record_ids=input_record_ids,
                model_version=model_version,
                narration_model_version=narration_model_version,
                timestamp=self.now,
            )
        )


def _add_coverage(builder: _Builder, coverage: CoverageReport) -> None:
    """Depend-on per `CoverageResult.depends_on_po_ids`, and Contradict per
    `InventoryContradiction` — the exact mapping coverage.py's own docstring
    already committed to."""
    for result in coverage.results:
        for po_id in result.depends_on_po_ids:
            builder.edge(
                "Depend-on",
                node("production_order", result.production_order_id),
                node("po", po_id),
                "coverage",
                [result.production_order_id, po_id, result.component_id],
                (
                    f"{result.production_order_id}'s coverage depends on {po_id}: "
                    f"days_of_coverage {result.days_of_coverage} vs on-hand floor "
                    f"{result.days_of_coverage_on_hand}, against a "
                    f"{result.days_to_deadline}-day deadline."
                ),
            )

    for contradiction in coverage.contradictions:
        builder.edge(
            "Contradict",
            node("warehouse", contradiction.component_id),
            node("erp", contradiction.component_id),
            "coverage",
            [contradiction.component_id],
            contradiction.note,  # verbatim
        )


def _add_monitor(builder: _Builder, monitor: MonitorReport) -> None:
    """Support from a poll to the event it detected. Polls that ran and
    found nothing get an explicit Update edge — same anti-ambiguity
    principle as item 6's no-trigger edge."""
    events_by_po: dict[str, list[str]] = {}
    for event in monitor.events:
        if event.po_id:
            events_by_po.setdefault(event.po_id, []).append(event.event_id)

    for decision in monitor.polled():
        detected = events_by_po.get(decision.po_id, [])
        if detected:
            for event_id in detected:
                builder.edge(
                    "Support",
                    node("poll", decision.po_id),
                    node("event", event_id),
                    "monitor",
                    # load_bearing_for is carried here rather than as separate
                    # Depend-on edges: those are COVERAGE's to emit (single
                    # owner, no duplicate assertion of the same dependency),
                    # but this poll's justification is only legible with them,
                    # and MONITOR's snapshot is the pre-disruption one that
                    # a later coverage pass no longer shows.
                    [decision.po_id, event_id, decision.component_id, *decision.load_bearing_for],
                    (
                        f"Proactive poll of {decision.po_id} read tracking "
                        f"'{decision.tracking_status}' against PO status "
                        f"'{decision.po_status}' — the evidence behind {event_id}. "
                        f"Polled because it was load-bearing for "
                        f"{', '.join(decision.load_bearing_for) or 'no order'} "
                        f"({decision.exposure_days} days of coverage resting on it)."
                    ),
                )
        else:
            builder.edge(
                "Update",
                node("poll", decision.po_id),
                node("po", decision.po_id),
                "monitor",
                [decision.po_id, decision.component_id],
                (
                    f"Polled {decision.po_id}: tracking '{decision.tracking_status}' is "
                    f"consistent with PO status '{decision.po_status}'. Checked and "
                    "clean — recorded so this is not mistaken for 'never checked'."
                ),
            )


def _add_verify(builder: _Builder, verification: VerificationReport) -> None:
    """Contradict (tracking vs claim), then Invalidate onto the reliability
    score. Both originate at the TRACKING node — AGENTS.md rule 4 made
    structural."""
    for check in verification.verifications:
        # model_version is item 3's real field, not a constant.
        claim_model_version = check.claim.model_version

        builder.decide(
            node("claim", check.claim.message_id),
            "verify",
            f"contradicted={check.contradicted}",
            [check.po_id, check.supplier_id, check.claim.message_id],
            model_version=claim_model_version,
        )

        if not check.contradicted:
            continue

        builder.edge(
            "Contradict",
            node("tracking", check.po_id),
            node("claim", check.claim.message_id),
            "verify",
            [check.po_id, check.claim.message_id, check.supplier_id],
            check.contradiction_reason or "",  # verbatim
            model_version=claim_model_version,
        )

        if check.reliability_change_reason is not None:
            builder.edge(
                "Invalidate",
                node("tracking", check.po_id),
                node("reliability", check.supplier_id),
                "verify",
                [check.po_id, check.supplier_id],
                (
                    f"{check.supplier_id}'s reliability_score moved "
                    f"{check.reliability_before} -> {check.reliability_after} "
                    f"({check.reliability_change_reason}). The tracking record is "
                    "the cause; nothing about the message's wording is."
                ),
                model_version=claim_model_version,
            )


def _add_disruption_to_plan(
    builder: _Builder,
    plan: Plan,
    coverage: Optional[CoverageReport],
    monitor: Optional[MonitorReport],
) -> None:
    """`Trigger` from each DisruptionEvent for this component to the plan
    that answers it.

    This is the edge that makes the graph one connected chain rather than
    several disconnected clusters: without it, MONITOR's poll and its event
    sit in their own island and there is no traceable route from "we
    detected the stall" to "here is what we decided about it" — which is the
    single thing an audit trail has to be able to show. The link is a real
    causal one (the plan exists because the disruption did), not a
    convenience: matched strictly on `component_id`, so an unrelated
    component's event never gets attributed to this plan."""
    events = []
    if coverage is not None:
        events.extend(coverage.events)
    if monitor is not None:
        events.extend(monitor.events)

    for event in events:
        if event.component_id != plan.component_id:
            continue
        builder.edge(
            "Trigger",
            node("event", event.event_id),
            node("plan", plan.plan_id),
            "planner",
            [event.event_id, plan.plan_id, plan.component_id],
            (
                f"{event.event_id} ({event.type}, detected via {event.source}) is a "
                f"disruption on {event.component_id} that {plan.plan_id} was built "
                "to answer."
            ),
        )


def _add_solver(builder: _Builder, solver_result: SolverResult) -> None:
    """Support per Rejection, citing the exact DropReason."""
    for rejection in solver_result.rejected:
        # Hard-filter reasons key on a supplier_id; "dominated" keys on a
        # combination label. Same Rejection shape, two subject kinds.
        subject_kind = "combination" if rejection.reason == "dominated" else "supplier"
        builder.edge(
            "Support",
            node(subject_kind, rejection.subject),
            node("rejection", rejection.subject),
            "solver",
            [rejection.subject, solver_result.component_id],
            f"[{rejection.reason}] {rejection.note}",  # DropReason + verbatim note
        )

    builder.decide(
        node("plan", solver_result.component_id),
        "solver",
        (
            f"pareto_set={len(solver_result.pareto_set)} candidate(s), "
            f"{len(solver_result.rejected)} rejected"
        ),
        [solver_result.component_id],
    )


def _add_planner(builder: _Builder, plan: Plan) -> None:
    """Invalidate onto every deadline-infeasible combination, plus the
    plan's own assumptions."""
    plan_node = node("plan", plan.plan_id)

    builder.edge(
        "Depend-on",
        plan_node,
        node("combination", plan.chosen_combination.label),
        "planner",
        [plan.plan_id, plan.component_id],
        (
            f"{plan.plan_id} sources from {plan.chosen_combination.label}: "
            f"total_cost {plan.total_cost}, lead {plan.chosen_combination.lead_time_days}d, "
            f"reliability {plan.chosen_combination.reliability_score}."
        ),
    )

    for alternative in plan.rejected_alternatives:
        if alternative.reason != "deadline_infeasible":
            continue
        # The specific order and day figures live in `regret` verbatim.
        # Re-parsing them out of that string would be exactly the
        # "recompute or restate" this module is required not to do.
        builder.edge(
            "Invalidate",
            node("deadline_constraint", plan.component_id),
            node("combination", alternative.option),
            "planner",
            [plan.plan_id, plan.component_id],
            alternative.regret,  # verbatim, names the order and the days
        )

    builder.assume(
        plan_node,
        plan.selection_rule,
        "planner",
        "Plan.selection_rule",
    )
    builder.assume(
        plan_node,
        plan.cost_of_inaction.baseline_note,
        "planner",
        "Plan.cost_of_inaction.baseline_note",
    )
    builder.assume(
        plan_node,
        plan.safety_stock_decision.reason,
        "planner",
        "Plan.safety_stock_decision.reason",
    )

    builder.decide(
        plan_node,
        "planner",
        (
            f"chose {plan.chosen_combination.label}, "
            f"deadline_feasible={plan.deadline_feasible}"
        ),
        [plan.plan_id, plan.component_id],
    )


def _add_erp_writes(
    builder: _Builder, plan: Plan, erp_writes: list[ERPUpdateResponse]
) -> None:
    """The irreversible action, in the trail.

    Every `POST /erp/update` the orchestrator performed for this plan gets an
    edge, so the audit trail shows not just what was decided but what was
    actually written — the one thing in this system that cannot be taken
    back (AGENTS.md rule 5). Two shapes:

      * `create_alternate_po` -> `Trigger` from the plan to the PO it caused
        to exist. `Trigger` rather than `Support` because the plan is the
        cause, not evidence: the PO exists BECAUSE the plan said so, and
        reading the chain forward (event -> plan -> po) is how an auditor
        traces a disruption all the way to the purchase it produced.
      * anything else (currently `store_plan`) -> `Update` from the plan to
        the write record. The plan's state was recorded; nothing new was
        caused.

    Note the direction is plan -> written thing in both cases, so the graph
    stays forward-traceable from MONITOR's poll through to the PO.
    """
    plan_node = node("plan", plan.plan_id)

    for write in erp_writes:
        if write.action == "create_alternate_po":
            created_po_id = write.resulting_state.get("po_id", write.update_id)
            builder.edge(
                "Trigger",
                plan_node,
                node("po", created_po_id),
                "ratchet",
                [plan.plan_id, created_po_id, write.update_id],
                (
                    f"{plan.plan_id} created {created_po_id} via "
                    f"{write.action} ({write.update_id}): "
                    f"{write.resulting_state.get('quantity')} units of "
                    f"{write.resulting_state.get('component_id')} from "
                    f"{write.resulting_state.get('supplier_id')} @ "
                    f"{write.resulting_state.get('unit_price')}, expected "
                    f"{write.resulting_state.get('expected_delivery')}. "
                    "IRREVERSIBLE (AGENTS.md rule 5)."
                ),
            )
        else:
            builder.edge(
                "Update",
                plan_node,
                node("erp_write", write.update_id),
                "ratchet",
                [plan.plan_id, write.update_id],
                (
                    f"{write.action} recorded {plan.plan_id} to the ERP as "
                    f"{write.update_id}. IRREVERSIBLE (AGENTS.md rule 5)."
                ),
            )


def _add_contingencies(builder: _Builder, plan: Plan, fired: list) -> None:
    """Item 9's fired fallbacks, in the trail.

    `Trigger` from the contingency to the plan it produced — the same
    relation and direction `_add_disruption_to_plan` uses for an event,
    because a fired contingency IS the cause of this plan existing. The note
    cites the `failure_trigger` BY NAME (its `kind` and its `condition`
    sentence), so an auditor reading the trail sees which pre-committed
    condition fired, not merely that something did.
    """
    for firing in fired:
        contingency = firing.contingency
        trigger = contingency.failure_trigger
        builder.edge(
            "Trigger",
            node("contingency", contingency.contingency_id),
            node("plan", plan.plan_id),
            "planner",
            [
                contingency.contingency_id,
                contingency.plan_id,
                trigger.subject,
                plan.plan_id,
            ],
            (
                f"{contingency.contingency_id} fired on failure_trigger "
                f"'{trigger.kind}' — {trigger.condition}. Observed "
                f"{trigger.observed_field}={firing.observed}, plan assumed "
                f"{trigger.plan_assumed}. Pre-committed fallback "
                f"{contingency.fallback_combination.label} was planned WITHOUT "
                "a fresh solve."
            ),
        )
        # What the fallback replaced — so the swap itself is auditable.
        builder.edge(
            "Invalidate",
            node("contingency", contingency.contingency_id),
            node("supplier", contingency.primary_action.supplier_id),
            "planner",
            [contingency.contingency_id, contingency.primary_action.supplier_id],
            (
                f"{contingency.primary_action.supplier_id} was committed "
                f"{contingency.primary_action.qty} units at "
                f"{contingency.primary_action.lead_time_days}-day lead time; "
                f"{trigger.kind} invalidated that commitment. The primary was "
                f"{contingency.primary_reversibility}, so it could still be "
                "swapped."
            ),
        )


def append_erp_write_edges(
    graph: ProvenanceGraph,
    plan: Plan,
    erp_writes: list[ERPUpdateResponse],
    *,
    now: Optional[datetime] = None,
) -> ProvenanceGraph:
    """Append ERP-write edges to a graph that was already built.

    Needed because an escalated plan's write happens LATER, at human
    approval, long after its graph was constructed. Rebuilding the whole
    graph then would need every upstream report again; appending the edges
    that actually became true is both cheaper and more honest — the earlier
    edges did not change, so they should not be recomputed.

    Idempotent: an `update_id` already cited in the graph is not added twice.
    """
    now = clock.now() if now is None else now
    already_cited = {
        record_id for edge in graph.edges for record_id in edge.input_record_ids
    }
    fresh = [w for w in erp_writes if w.update_id not in already_cited]
    if not fresh:
        return graph

    builder = _Builder(now)
    _add_erp_writes(builder, plan, fresh)
    graph.edges.extend(builder.edges)
    return graph


def _add_ratchet(builder: _Builder, brief: DecisionBrief, plan: Optional[Plan]) -> None:
    """Trigger per fired condition; one explicit Update when none fired."""
    brief_key = brief.plan_id or brief.component_id
    brief_node = node("brief", brief_key)

    if plan is not None:
        builder.edge(
            "Support",
            node("plan", plan.plan_id),
            brief_node,
            "ratchet",
            [plan.plan_id, brief.component_id],
            (
                f"{plan.plan_id} is the plan this decision was taken on: "
                f"total_cost {plan.total_cost} against threshold "
                f"{brief.approval_threshold}."
            ),
        )

    if brief.triggers_fired:
        for trigger in brief.triggers_fired:
            builder.edge(
                "Trigger",
                node("trigger", trigger),
                brief_node,
                "ratchet",
                [brief_key, brief.component_id],
                (
                    f"Hard escalation trigger '{trigger}' fired (AGENTS.md rule 3) — "
                    f"decision forced to '{brief.decision}'. Non-overridable."
                ),
            )
    else:
        builder.edge(
            "Update",
            node("ratchet_check", brief.component_id),
            brief_node,
            "ratchet",
            [brief_key, brief.component_id],
            (
                "All three hard triggers evaluated (cost_above_threshold, "
                "no_feasible_deadline_plan, quality_risk); none fired, so the "
                f"decision is '{brief.decision}'. Recorded explicitly so that no "
                "trigger firing is never ambiguous with never having checked."
            ),
        )

    builder.assume(
        brief_node,
        brief.falsification_line,
        "ratchet",
        "DecisionBrief.falsification_line",
    )

    # The decision is deterministic; only the narration may be LLM-authored.
    # See the module docstring's model_version section for why these are two
    # fields and not one.
    builder.decide(
        brief_node,
        "ratchet",
        brief.decision,
        [brief_key, brief.component_id],
        model_version=DETERMINISTIC_MODEL_VERSION,
        narration_model_version=brief.model_version,
    )


def _build_tool_calls(
    monitor: Optional[MonitorReport],
    verification: Optional[VerificationReport],
    solver_result: Optional[SolverResult],
    brief: Optional[DecisionBrief],
) -> ToolCallSummary:
    summary = ToolCallSummary()
    notes: list[str] = []

    if monitor is not None:
        summary.monitor_polls_made = monitor.polls_made
        summary.monitor_polls_available = monitor.polls_available
        skipped = monitor.polls_available - monitor.polls_made
        summary.calls_avoided_by_gating += skipped
        notes.append(
            f"MONITOR: {monitor.polls_made} tracking poll(s) of "
            f"{monitor.polls_available} open PO(s); {skipped} skipped as not "
            "load-bearing."
        )

    if verification is not None:
        summary.verify_probes_made = verification.probes_made
        summary.verify_probes_reused_from_monitor = verification.probes_reused_from_monitor
        summary.calls_avoided_by_gating += verification.probes_reused_from_monitor
        notes.append(
            f"VERIFY: {verification.probes_made} new tracking probe(s); "
            f"{verification.probes_reused_from_monitor} reused MONITOR's read of "
            "the same PO in the same cycle."
        )

    if solver_result is not None:
        summary.solver_quotes_requested = solver_result.quotes_requested
        summary.solver_quotes_reused = solver_result.quotes_reused
        summary.calls_avoided_by_gating += solver_result.quotes_reused
        notes.append(
            f"SOLVER: {solver_result.quotes_requested} RFQ call(s); "
            f"{solver_result.quotes_reused} reused a still-valid quote. "
            "Hard-filtered suppliers were never quoted at all."
        )

    if brief is not None:
        summary.ratchet_approval_checks_made = brief.approval_checks_made
        notes.append(
            f"RATCHET: {brief.approval_checks_made} approval check(s)."
        )

    summary.total_calls_made = (
        summary.monitor_polls_made
        + summary.verify_probes_made
        + summary.solver_quotes_requested
        + summary.ratchet_approval_checks_made
    )
    summary.notes = notes
    return summary


def build_provenance_graph(
    *,
    coverage: Optional[CoverageReport] = None,
    monitor: Optional[MonitorReport] = None,
    verification: Optional[VerificationReport] = None,
    solver_result: Optional[SolverResult] = None,
    plan: Optional[Plan] = None,
    brief: Optional[DecisionBrief] = None,
    erp_writes: Optional[list[ERPUpdateResponse]] = None,
    fired_contingencies: Optional[list] = None,
    now: Optional[datetime] = None,
) -> ProvenanceGraph:
    """Build the graph from whichever stages actually ran. Every argument is
    optional so a partial pipeline produces a partial — but still valid and
    still traceable — graph, rather than requiring a full run to record
    anything.

    Deterministic: same inputs, same edges, same ids (given a sequence
    reset). No LLM is consulted; `model_version` is read off the records,
    never chosen here.
    """
    now = clock.now() if now is None else now
    builder = _Builder(now)

    if coverage is not None:
        _add_coverage(builder, coverage)
    if monitor is not None:
        _add_monitor(builder, monitor)
    if verification is not None:
        _add_verify(builder, verification)
    if solver_result is not None:
        _add_solver(builder, solver_result)
    if plan is not None:
        _add_disruption_to_plan(builder, plan, coverage, monitor)
        _add_planner(builder, plan)
    if brief is not None:
        _add_ratchet(builder, brief, plan)
    if plan is not None and fired_contingencies:
        _add_contingencies(builder, plan, fired_contingencies)
    if plan is not None and erp_writes:
        _add_erp_writes(builder, plan, erp_writes)

    return ProvenanceGraph(
        built_at=now,
        edges=builder.edges,
        assumptions=builder.assumptions,
        decisions=builder.decisions,
        # PLANNER's own instances, by reference — never copies. See the
        # module docstring's "Regret" section.
        regret_ledger=plan.rejected_alternatives if plan is not None else [],
        cost_of_inaction=plan.cost_of_inaction if plan is not None else None,
        tool_calls=_build_tool_calls(monitor, verification, solver_result, brief),
    )
