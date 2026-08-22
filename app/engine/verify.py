"""VERIFY — ARCHITECTURE.md §4 item 3, §3's third stage. Serves Supplier Risk
Handling (15%, PROJECT.md §2).

Deterministic comparison and reliability arithmetic; parsing is the only step
that may call an LLM, and even that has a required non-LLM path. AGENTS.md
rule 1: the LLM's two jobs anywhere in this repo are (a) parsing unstructured
text into structured fields and (b) turning a finished decision into plain
language. Extracting `claim_status` from a supplier's message is job (a) —
nothing else in this file is. Whether a claim contradicts tracking, and how
much a reliability score moves, are both plain Python over structured fields;
no LLM is consulted for either.

--------------------------------------------------------------------------
What this module does, precisely
--------------------------------------------------------------------------
1. Parses a supplier's free-text claim into a `claim_status` (dispatched /
   delayed / cancelled / confirmed / unclear) and a delay-day count. LLM when
   `TRACE_LLM_ENABLED`, a regex/keyword parser otherwise — and the regex path
   is REQUIRED regardless of the flag (AGENTS.md rule 2): if the LLM call
   raises for any reason, this falls through to the deterministic parser
   rather than propagating the failure, because the pipeline losing Gemini
   mid-demo must not mean the pipeline stops (PROJECT.md §6's own mitigation:
   "flip the flag and keep going" — this file makes that flip unnecessary for
   a single failed call, though a demo should still rehearse the LLM-off run).
2. Compares that claim against the PO's tracking record. Pure structured
   comparison — claim_status vs tracking_status vs last_movement. Never reads
   the message's tone, hedging, or wording; AGENTS.md rule 4 is not a style
   preference here, it's enforced by never handing message text to the
   comparison function at all — `_is_contradicted` takes a `ClaimStatus`
   enum value, not a string.
3. On a contradiction (or a confirmed delivery), updates the supplier's
   reliability score via the exponentially weighted reliability update:
   `B(t+1) = (1-λ)B(t) + λs(t+1)`. `s(t+1)` is drawn only from what tracking
   showed, never from anything about how the claim was phrased.

--------------------------------------------------------------------------
Gating: probe only when a decision depends on the claim
--------------------------------------------------------------------------
This module verifies claims for the SAME set `coverage.polling_targets()`
already names — load-bearing POs, the ones whose withdrawal alone would turn
some production order critical (OPEN_ITEMS.md, signed off, not re-derived
here). That set IS "a decision depends on the claim": coverage's own
`days_of_coverage` for a real production order is resting on this specific
delivery arriving as promised.

Tool efficiency goes one step further than gating which POs to look at: when
composed with a `MonitorReport` (item 2b already having pulled tracking for
these exact POs in the same cycle), this module REUSES that read instead of
issuing a second `GET /tracking/{po_id}` for the same PO in the same pass.
Calling `run_verification_cycle` without a `monitor` argument still works
(useful for standalone testing) but an orchestrator wiring 2b and 3 together
should always pass one, or it pays for the same tracking lookup twice.

--------------------------------------------------------------------------
Resolving OPEN_ITEMS.md: does "dependable inbound" couple to reliability?
--------------------------------------------------------------------------
No, and it should not — "dependable inbound" stays reliability-independent.
`coverage.py`'s `dependable_inbound()` filters purchase orders on PO status
alone (a filter this module does not name here, to avoid this docstring
silently going stale if that constant is ever renamed) — it never reads
`SupplierRecord.reliability_score`, and this module does not change that.
Confirmed by reading the function, not assumed;
see `tests/test_verify.py::test_dependable_inbound_stays_reliability_independent`
for the check that keeps this true.

Two reasons this is a decision, not a gap:

1. ARCHITECTURE.md §3 already gives `days_of_coverage` a specific, load-
   bearing meaning: "what we have if suppliers keep their word." The GAP
   between it and `days_of_coverage_on_hand` IS the exposure to supplier
   claims — BRAND.md §3 names this as the project's own thesis. If
   `dependable_inbound` started excluding POs from low-reliability suppliers,
   `days_of_coverage` would stop meaning "if suppliers keep their word" — it
   would become a reliability-weighted blend, and "the gap is the exposure"
   stops being a sentence anyone could defend in Q&A, because the gap would
   already have reliability baked into one side of it.
2. `reliability_score` is a supplier-level rolling average — this module's
   own output. Using it to gate one specific PO in or out of a stock
   projection means picking a cutoff on that average, and that cutoff has no
   source in the problem statement — an invented threshold (AGENTS.md rule
   7). The already-specified consumer of reliability is the Pareto SOLVER
   (ARCHITECTURE.md §3: "price, lead time, reliability, quality, MOQ,
   available qty"), which weighs it explicitly, per decision. Coverage math
   is not that consumer.

A DIFFERENT coupling — once THIS SPECIFIC PO is confirmed contradicted (not a
reliability average, a fact about one shipment), should coverage stop
crediting it? That is plausible and NOT decided here: it belongs to item 8
(staleness detector, "re-enter at the earliest invalidated stage") re-running
COVERAGE with updated environment state after VERIFY produces a confirmed
contradiction — not to coverage.py permanently growing a contradiction-aware
filter. Logged in OPEN_ITEMS.md for whoever builds item 8, not implemented
here.

--------------------------------------------------------------------------
Naming
--------------------------------------------------------------------------
The recurrence `B(t+1) = (1-λ)B(t) + λs(t+1)` is called "the exponentially
weighted reliability update" everywhere in this file — in prose, in variable
names, in the one log-shaped record it produces. Never anything else.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

from app import config
from app.engine.coverage import CoverageReport, compute_coverage
from app.engine.monitor import (
    NOT_MOVED_TRACKING_STATUSES,
    MonitorReport,
    run_monitor_cycle,
)
from app.environment.clock import clock
from app.environment.schemas import SupplierMessage
from app.environment.seed_data import STATE, Store

ClaimStatus = Literal["dispatched", "delayed", "cancelled", "confirmed", "unclear"]

# ARCHITECTURE.md §7's two given reasons a reliability score is allowed to
# move. Deliberately not extended: a third "no news" bucket for claims that
# are consistent with tracking but not yet delivered is real, but crediting a
# supplier for merely "hasn't been caught lying yet" would be inventing a
# signal the spec never gave a name to (AGENTS.md rule 7) — see
# `_outcome_score`, which returns None for exactly that case.
ReliabilityChangeReason = Literal["tracking_contradiction", "confirmed_delivery"]

# The exponentially weighted reliability update's learning rate. 0.75 (SUP-21's
# seed score) with one tracking_contradiction (s=0.0) lands at 0.45 — visible
# on a coverage board without a single bad tracking read wiping out a
# supplier's entire history. This is a coefficient of a formula
# ARCHITECTURE.md §3/§4 already specifies, not an invented metric in its own
# right (AGENTS.md rule 7 is about inventing scores, not about picking a
# smoothing constant for a named, agreed-on recurrence).
RELIABILITY_LAMBDA = 0.4

# Tracking statuses meaning "the shipment has not moved." Imported from
# monitor.py, not redefined — one definition, and MONITOR already owns it.
_NOT_MOVED = NOT_MOVED_TRACKING_STATUSES


# ==========================================================================
# Claim parsing — the one place an LLM may be consulted
# ==========================================================================

# Checked in this order because a real message can mention more than one
# thing at once. Task Zero's own smoke message — "running about 3 days
# behind... We've dispatched it as of this morning though" — mentions both a
# delay and a dispatch, and Gemini classified it as "dispatched" with
# claimed_delay_days=3 (OPEN_ITEMS.md, the 2026-08-22 run). Checking dispatch
# before delay keeps the deterministic parser's answer in lockstep with that
# precedent rather than picking the opposite reading.
_CANCEL_RE = re.compile(r"\bcancel", re.IGNORECASE)
_DISPATCH_RE = re.compile(r"\b(dispatch(?:ed|ing)?|shipped|picked up)\b", re.IGNORECASE)
_DELAY_RE = re.compile(r"\b(delay(?:ed)?|running\s+\S+\s+behind|\bbehind\b|\blate\b)\b", re.IGNORECASE)
# "confirmed" requires positive delivery-status language, not a bare
# "please confirm" (seed_data.py's own template pool has one of those — "please
# confirm the PO to proceed" — which is a request TO us, not a status claim,
# and must not be misread as one).
_CONFIRMED_RE = re.compile(r"\b(on schedule|as planned|unchanged|no delay)\b", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*(?:-\s*(\d+)\s*)?\s*day", re.IGNORECASE)


def _extract_claim_status_deterministic(message_body: str) -> ClaimStatus:
    """Keyword parsing only — the claim_status enum, nothing about tone.
    AGENTS.md rule 4: if this function ever grows a check for hedging
    language, adjectives, or sentiment, that is the rule being broken, not a
    judgement call. It extracts WHAT was claimed, never HOW it was said."""
    if _CANCEL_RE.search(message_body):
        return "cancelled"
    if _DISPATCH_RE.search(message_body):
        return "dispatched"
    if _DELAY_RE.search(message_body):
        return "delayed"
    if _CONFIRMED_RE.search(message_body):
        return "confirmed"
    return "unclear"


def _extract_delay_days_deterministic(message_body: str) -> int:
    """First `<N> day(s)` or `<N>-<M> day(s)` mention; the upper bound of a
    range, so the extracted figure is the conservative (longer) reading. 0
    when no delay is mentioned at all."""
    match = _DAYS_RE.search(message_body)
    if not match:
        return 0
    low = int(match.group(1))
    high = int(match.group(2)) if match.group(2) else low
    return max(low, high)


class ParsedClaim(BaseModel):
    """A supplier's claim, reduced to exactly two structured facts: what they
    said the status was, and how many days of delay (if any) they admitted
    to. `raw_message` and `parsed_by` exist for audit traceability — so a
    contradiction can be traced back to the actual message and the actual
    parser that read it — not so anything downstream re-reads the text for
    tone.

    `model_version` is ARCHITECTURE.md §7's `ProvenanceEdge.model_version`
    field, in its exact given format (`"gemini-<version> | deterministic"`) —
    carried here so item 7 reads it off this record instead of re-deriving or
    hardcoding a model string of its own. It is the one field in this whole
    module that would catch a SILENT demo-time degradation: if
    `TRACE_LLM_ENABLED` is true but a specific verification's
    `model_version` reads `"deterministic"`, the LLM call failed on that
    message and fell through (see `parse_claim`) — `parsed_by` says the same
    thing at a coarser grain, `model_version` is the one with the literal
    string an auditor or judge can match against what the demo was supposed
    to be running.
    """

    po_id: Optional[str]
    claim_status: ClaimStatus
    claimed_delay_days: int = 0
    parsed_by: Literal["deterministic", "llm"]
    model_version: str
    raw_message: str
    message_id: str


DETERMINISTIC_MODEL_VERSION = "deterministic"


def parse_claim(message: SupplierMessage) -> ParsedClaim:
    """Parse one supplier message into a `ParsedClaim`. LLM path first when
    `TRACE_LLM_ENABLED`; any failure there — network, quota, a malformed
    response — falls through to the deterministic path rather than raising,
    because AGENTS.md rule 2 makes the deterministic path the one this
    pipeline cannot function without, and a parse failure must degrade to it
    silently rather than break claim verification entirely.

    `model_version` on the returned claim is set from `parse_claim`'s own
    control flow, not guessed after the fact: it is only ever
    `gemini_client.MODEL_VERSION` on the branch that actually returned
    successfully from Gemini, and `DETERMINISTIC_MODEL_VERSION` on every
    other branch — including the fallthrough after a caught exception. A
    demo running with `TRACE_LLM_ENABLED=true` that silently loses the
    network mid-run will show `model_version="deterministic"` on the affected
    verifications; that flip is the signal (see `ParsedClaim`'s docstring).
    """
    if config.TRACE_LLM_ENABLED:
        try:
            from app.llm import gemini_client

            parsed = gemini_client.parse_supplier_claim(message.body)
            return ParsedClaim(
                po_id=parsed.po_id or message.po_id,
                claim_status=parsed.claim_status,  # type: ignore[arg-type]
                claimed_delay_days=parsed.claimed_delay_days,
                parsed_by="llm",
                model_version=gemini_client.MODEL_VERSION,
                raw_message=message.body,
                message_id=message.message_id,
            )
        except Exception:
            # AGENTS.md rule 2 — the LLM is never load-bearing. Fall through
            # to the required deterministic path below.
            pass

    return ParsedClaim(
        po_id=message.po_id,
        claim_status=_extract_claim_status_deterministic(message.body),
        claimed_delay_days=_extract_delay_days_deterministic(message.body),
        parsed_by="deterministic",
        model_version=DETERMINISTIC_MODEL_VERSION,
        raw_message=message.body,
        message_id=message.message_id,
    )


# ==========================================================================
# Claim vs tracking — pure structured comparison
# ==========================================================================


def _is_contradicted(
    claim_status: ClaimStatus, tracking_status: str, last_movement: Optional[str]
) -> tuple[bool, Optional[str]]:
    """Compare a claim's status enum to a tracking status string. Nothing
    else is passed in — not the message body, not the supplier's name, not
    anything that could smuggle tone back into this decision.

    Two rules, both citing only tracking facts in the returned reason
    (AGENTS.md rule 4 — the reason a score moves is always a tracking record,
    never how an email read):

    - claim "dispatched" or "confirmed" (i.e. the supplier is asserting
      movement or normalcy) + tracking shows no movement -> contradicted.
      This is PO-7712: SUP-21 claims dispatched, tracking reads
      label_created_no_pickup.
    - claim "cancelled" + tracking shows movement -> contradicted the other
      way (they said it stopped; the carrier says it's moving).

    "delayed" and "unclear" are never flagged here — a delay claim consistent
    with unmoved tracking is not a contradiction, it is corroboration, and
    "unclear" carries no assertion to check. This is a deliberate, narrow rule
    set, not an unhandled gap: extending it to more claim/tracking
    combinations without a concrete scenario behind them would be inventing
    cases (AGENTS.md rule 7).
    """
    moved = tracking_status not in _NOT_MOVED

    if claim_status in ("dispatched", "confirmed") and not moved:
        movement_note = f" (last_movement={last_movement})" if last_movement else ""
        return True, (
            f"claim was '{claim_status}', but tracking status is "
            f"'{tracking_status}'{movement_note} — the shipment has not moved."
        )

    if claim_status == "cancelled" and moved:
        movement_note = f" (last_movement={last_movement})" if last_movement else ""
        return True, (
            f"claim was 'cancelled', but tracking status is "
            f"'{tracking_status}'{movement_note} — the shipment is moving."
        )

    return False, None


def _outcome_score(
    contradicted: bool, tracking_status: str
) -> Optional[tuple[float, ReliabilityChangeReason]]:
    """s(t+1) for the exponentially weighted reliability update, plus the
    spec's own reason label (ARCHITECTURE.md §7) it is attributed to. `None`
    means no update fires at all.

    Only two things move a score: a tracking contradiction (s=0.0) or a
    confirmed delivery (s=1.0, only when tracking itself says `delivered`).
    A claim that is merely uncontradicted-so-far — dispatched and tracking
    shows in_transit, say — returns None on purpose. Crediting "hasn't been
    caught lying yet" as positive evidence would be inventing a signal
    (AGENTS.md rule 7); the spec's own ReliabilityRecord only names two
    reasons a score changes, and this function does not add a third.
    """
    if contradicted:
        return 0.0, "tracking_contradiction"
    if tracking_status == "delivered":
        return 1.0, "confirmed_delivery"
    return None


# ==========================================================================
# Output shapes
# ==========================================================================


class ReliabilityRecord(BaseModel):
    """ARCHITECTURE.md §7, verbatim. One snapshot of one exponentially
    weighted reliability update."""

    supplier_id: str
    score: float
    last_updated: datetime
    last_change_reason: ReliabilityChangeReason


class ClaimVerification(BaseModel):
    """One load-bearing PO, one claim, one verdict."""

    po_id: str
    supplier_id: str
    component_id: str
    claim: ParsedClaim
    tracking_status: str
    last_movement: Optional[str] = None
    contradicted: bool
    contradiction_reason: Optional[str] = None
    reliability_before: float
    reliability_after: float
    reliability_change_reason: Optional[ReliabilityChangeReason] = None
    # Cross-reference to a MonitorReport event covering the same PO, when one
    # exists — item 7's provenance graph links these rather than treating them
    # as two unrelated findings.
    related_monitor_event_id: Optional[str] = None
    probe_source: Literal["reused_from_monitor", "own_probe"]
    verified_at: datetime


class VerificationSkip(BaseModel):
    """A load-bearing PO that was NOT verified, and why. Recorded so the
    tool-efficiency accounting can show what was deliberately passed over —
    not just what was checked."""

    po_id: str
    reason: Literal["no_supplier_claim_on_record", "no_tracking_record"]


class VerificationReport(BaseModel):
    """One VERIFY cycle."""

    verified_at: datetime
    verifications: list[ClaimVerification]
    skipped: list[VerificationSkip]
    reliability_changes: list[ReliabilityRecord]
    # Tool efficiency (10%): calls this cycle actually issued vs calls it got
    # for free by reusing MONITOR's read of the same PO in the same pass.
    probes_made: int
    probes_reused_from_monitor: int

    def for_po(self, po_id: str) -> Optional[ClaimVerification]:
        for verification in self.verifications:
            if verification.po_id == po_id:
                return verification
        return None

    def contradictions(self) -> list[ClaimVerification]:
        return [v for v in self.verifications if v.contradicted]


# ==========================================================================
# The cycle
# ==========================================================================


def run_verification_cycle(
    store: Optional[Store] = None,
    *,
    coverage: Optional[CoverageReport] = None,
    monitor: Optional[MonitorReport] = None,
    now: Optional[datetime] = None,
) -> VerificationReport:
    """Verify supplier claims for every load-bearing PO that has one on
    record. Gated on `coverage.polling_targets()` — the same set item 2b
    polls, because that set IS "a decision depends on the claim"
    (ARCHITECTURE.md §4 item 3's own wording): coverage.py's own
    `days_of_coverage` for a real production order rests on exactly these
    deliveries arriving as promised.

    Pass `coverage` / `monitor` reports an orchestrator already computed to
    avoid recomputing them (and, for `monitor`, to avoid a second
    `GET /tracking/{po_id}` for a PO it already polled this cycle). Omitting
    either computes a fresh one — useful standalone, costlier if this is run
    right after item 2b already ran.
    """
    store = STATE if store is None else store
    now = clock.now() if now is None else now
    coverage = compute_coverage(store, now=now) if coverage is None else coverage
    monitor = (
        run_monitor_cycle(store, coverage=coverage, now=now)
        if monitor is None
        else monitor
    )

    targets = coverage.polling_targets()

    verifications: list[ClaimVerification] = []
    skipped: list[VerificationSkip] = []
    reliability_changes: list[ReliabilityRecord] = []
    probes_made = 0
    probes_reused = 0

    for po_id in targets:
        po = store.get_purchase_order(po_id)
        if po is None:
            continue

        inbound = [
            m for m in store.list_inbox(po_id=po_id) if m.direction == "inbound"
        ]
        if not inbound:
            skipped.append(
                VerificationSkip(po_id=po_id, reason="no_supplier_claim_on_record")
            )
            continue
        # list_inbox sorts by sent_at ascending — the latest claim on file.
        message = inbound[-1]

        claim = parse_claim(message)

        monitor_decision = monitor.for_po(po_id)
        related_event_id = next(
            (e.event_id for e in monitor.events if e.po_id == po_id), None
        )
        if monitor_decision is not None and monitor_decision.polled:
            # Reuse MONITOR's tracking read — no second call for this PO.
            tracking_status = monitor_decision.tracking_status
            last_movement = monitor_decision.last_movement
            probe_source: Literal["reused_from_monitor", "own_probe"] = (
                "reused_from_monitor"
            )
            probes_reused += 1
        else:
            tracking = store.get_tracking(po_id)
            if tracking is None:
                skipped.append(
                    VerificationSkip(po_id=po_id, reason="no_tracking_record")
                )
                continue
            tracking_status = tracking.tracking_status
            last_movement = tracking.last_movement
            probe_source = "own_probe"
            probes_made += 1

        contradicted, reason = _is_contradicted(
            claim.claim_status, tracking_status, last_movement
        )
        outcome = _outcome_score(contradicted, tracking_status)

        reliability_before = store.suppliers[po.supplier_id].reliability_score
        if outcome is not None:
            score, change_reason = outcome
            record = _update_reliability(
                store, po.supplier_id, score, change_reason, now
            )
            reliability_changes.append(record)
            reliability_after = record.score
        else:
            change_reason = None
            reliability_after = reliability_before

        verifications.append(
            ClaimVerification(
                po_id=po_id,
                supplier_id=po.supplier_id,
                component_id=po.component_id,
                claim=claim,
                tracking_status=tracking_status,
                last_movement=last_movement,
                contradicted=contradicted,
                contradiction_reason=reason,
                reliability_before=reliability_before,
                reliability_after=reliability_after,
                reliability_change_reason=change_reason,
                related_monitor_event_id=related_event_id,
                probe_source=probe_source,
                verified_at=now,
            )
        )

    return VerificationReport(
        verified_at=now,
        verifications=verifications,
        skipped=skipped,
        reliability_changes=reliability_changes,
        probes_made=probes_made,
        probes_reused_from_monitor=probes_reused,
    )


def _update_reliability(
    store: Store,
    supplier_id: str,
    outcome_score: float,
    reason: ReliabilityChangeReason,
    now: datetime,
) -> ReliabilityRecord:
    """The exponentially weighted reliability update:
    `B(t+1) = (1-λ)B(t) + λs(t+1)`. Provenance only — `outcome_score` and
    `reason` are both derived purely from a tracking comparison
    (`_is_contradicted` / `_outcome_score`), never from anything about how a
    message was worded.

    Mutates the live `SupplierRecord.reliability_score` in `store` — this IS
    the persistent memory ARCHITECTURE.md §7 calls "VERIFY's persistent
    memory per supplier." It is not AGENTS.md rule 5's one irreversible
    action; that label belongs to `POST /erp/update` alone. This is an
    in-memory environment update, freely repeatable, and reset by
    `build_store()`'s clean-slate guarantee (ARCHITECTURE.md §8) like every
    other field on a fresh `Store`.
    """
    supplier = store.suppliers[supplier_id]
    before = supplier.reliability_score
    after = round(
        (1 - RELIABILITY_LAMBDA) * before + RELIABILITY_LAMBDA * outcome_score, 4
    )
    supplier.reliability_score = after
    return ReliabilityRecord(
        supplier_id=supplier_id,
        score=after,
        last_updated=now,
        last_change_reason=reason,
    )
