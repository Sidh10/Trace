# ARCHITECTURE.md

The technical build reference for TRACE. Read `AGENTS.md` first for the rules
this architecture is required to satisfy, and `PROJECT.md` for why any of this
matters. This file covers **how it's built**, only.

---

## 1. Stack

```
Python 3.11 + FastAPI       simulated environment + agent service
SQLite                       state, supplier reliability memory, audit log
Gemini free tier             function calling — see §2, Task Zero
Single static HTML file      coverage board + judge panel, static/index.html
```

**Rejected, do not add:** OR-Tools, MILP solvers, LangGraph, PostgreSQL,
NetworkX / knowledge graphs, React, React Flow, npm, any build step, multi-agent
frameworks, blockchain / hash-chained ledgers.

*Why:* the search space is one component across ~10–20 suppliers with an MOQ
constraint — brute-force enumeration over supplier pairs is **also exact**,
builds in a fraction of the time, and carries no "explain your formulation"
risk in Q&A. A knowledge graph models depth the data doesn't have — production
records carry `component_required_per_unit`, a single level, no multi-tier BOM.

---

## 2. TASK ZERO — before anything else

Prove a **Gemini function-calling round trip** end to end: one script, one
tool, one successful structured response. `scripts/task_zero_gemini_check.py`.

Nothing else starts until this passes or fails. If it fails, AGENTS.md rule 2
(LLM-optional mode) means the project still has a working submission — but the
team needs to know within the first hour, not the twelfth.

---

## 3. Control flow

A bounded loop — not a straight pipeline, not an open agent loop. See
AGENTS.md rule 6 for why both of those fail.

```
   ┌──────────────────────────────────────────────────────────────┐
   │  SIMULATED ENVIRONMENT (ours, spec schemas verbatim)          │
   │  inventory · POs · suppliers · production · inbox · tracking  │
   │  + simulated clock                                            │
   └───────────────────────────┬──────────────────────────────────┘
                               ▼
   MONITOR ─── gated poll on LOAD-BEARING POs — those whose withdrawal alone
      │        turns some production order critical (not "thin-coverage POs";
      │        at Beat 1 PROD-882 reads healthy and the poll must still fire)
      ▼
   COVERAGE ENGINE (deterministic) — two metrics, both reported
      │        days_of_coverage_on_hand = usable_stock ÷ daily_usage
      │            → what we actually hold
      │        days_of_coverage = on-hand + dependable inbound POs
      │            → what we have if suppliers keep their word
      │        THE GAP BETWEEN THEM IS THE EXPOSURE TO SUPPLIER CLAIMS
      │        cross-ref production priority + deadline
      │        detect ERP-vs-warehouse mismatch, log as contradiction
      ▼
   TOOL GATE ── precondition check: is this call actually needed?
      ▼
   VERIFY ──── supplier claim vs tracking; contradiction →
      │        exponentially-weighted reliability downgrade (provenance only)
      ▼
   HARD FILTER ─ drop uncertified / budget-infeasible candidates
      │          BEFORE spending an RFQ call
      ▼
   SOLVER ──── Pareto non-domination over price, lead time, reliability,
      │        quality, MOQ, available qty. No weighted-sum collapse.
      ▼
   PLAN ────── multi-action: supplier split + stock allocation +
      │        safety-stock draw (with justification) + production reschedule
      ▼
   RATCHET ─── hard escalation triggers (AGENTS.md rule 3). Execute or escalate.
      ▼
   ERP WRITE ─ the one irreversible action
      ▼
   AUDIT ───── provenance graph (§4, item 7)

   ▲                                                     │
   └──── STALENESS DETECTOR: re-enter at the earliest ────┘
         invalidated stage, not from the top.
         Then POST-REPLAN VERIFICATION: does the new plan still hold?
```

---

## 4. Components — build in this order

Everything above the cut-line works end to end before anything below it starts.

| # | Component | Serves | Est. |
|---:|---|---|---:|
| 0 | **Task zero: Gemini function-call round trip** | blocking | ✅ passed |
| 1 | Simulated env: spec schemas verbatim, REST surface (§6), simulated clock | enabler | ✅ built |
| 2 | Coverage engine: **two metrics** — `days_of_coverage_on_hand` (usable ÷ daily usage) and `days_of_coverage` (+ dependable inbound). Thresholds are spec-field comparisons, never invented constants: `critical` = coverage < days_to_deadline; `at_risk` = projection dips below `safety_stock` before deadline. Plus ERP-vs-warehouse mismatch detection | 35% | ✅ built |
| 2b | Gated polls on **load-bearing** POs — withdrawal alone turns some order critical. Broader than "thin-coverage POs" by design; the thin gate misses PO-7712 at Beat 1 | 35% + 10% | ✅ built |
| 3 | Claim verification vs tracking; provenance-only reliability update, exponentially weighted `B(t+1) = (1−λ)B(t) + λs(t+1)`; probe only when a decision depends on the claim | 15% | ✅ built |
| 4 | Hard pre-filter (cert + budget) → Pareto solver; quote expiry (`quote_valid_hours: 6`) as a real constraint | 20% | ✅ built |
| 5 | Multi-action recovery plans: supplier split + stock allocation + **production reschedule** (not safety stock — that's 5b) | 35% | ✅ built |
| 5b | Safety-stock consumption as a solver action, gated on written justification | 35% + 20% | ✅ built |
| 6 | Hard escalation ratchet + decision brief: cost delta, alternatives, **cost of no action**, **what would have to be true for this to be wrong** | 20% | ✅ built |
| 7 | Provenance graph — audit trail and assumption ledger as ONE object (Support / Depend-on / Contradict / Invalidate / Trigger / Update) + regret-scored rejected alternatives + model version per decision | 10% + 20% | ✅ built |
| 7b | **Orchestrator** (`app/api/routes.py` + `app/main.py`) — items 1-7 assembled into one callable chain, `POST /agent/handle-event`; **owns the single ERP-write boundary** (rule 5) and the human-approval step `POST /agent/approval/{plan_id}` | enabler | ✅ built |
| | **── CUT-LINE. At hour 12, ship exactly the above. ──** | | |
| 8 | Staleness detector + earliest-conflict re-entry + post-replan verification (`app/engine/staleness.py`; re-entry executed by the orchestrator) | 10% | ✅ built |
| 9 | Contingency plans with explicit triggers `{primary, failure_trigger, fallback}` | 10% | ✅ built |
| 10 | Tool-call precondition logging + count-vs-necessity summary in audit trail | 10% | 45m |
| 11 | **Judge-controlled disruption panel** (single HTML file) | demo | 1h |
| 12 | Coverage board — days of coverage per production order, live | demo | 1.5h |
| 13 | **Multi-baseline comparison harness** — see §11 below | demo | 3h |

**Items 1–9 are ✅ COMPLETE — the cut-line is met, and the re-entry spine is
now assembled through contingency fallback.** Items 10–13 are the remaining
difference between placing and winning.

**Parallelisation (3–4 people, own Claude accounts):**
- **A** — item 1 (environment + clock), hand off early, help B/C, then 11/12
- **B** — 2 / 2b / 5 / 5b (the continuity spine)
- **C** — 3 / 4 / 6 (the decision spine)
- **D** — 7, then 8 / 9 / 10, then 13

Per-person estimated total: 5.25–6h each — balanced, but not evenly *timed*.
A finishes item 1 (3h) early and should actively help B or C rather than sit
idle until 11/12 start. Integrate at hour 8, not hour 15.

---

## 5. What the simulator may and may not contain

We build the simulator, so this boundary matters more than usual — see
AGENTS.md rule 8.

**Permitted — the problem statement specifies these:**
- Simulated clock and time pressure (§4.7; Scenario 6 "12 simulated hours"; §17 "recheck after 6 simulated hours")
- Quote expiry — the RFQ schema ships `quote_valid_hours: 6`
- Tracking that contradicts a supplier claim (§5.10, `label_created_no_pickup`)
- Stale ERP inventory vs warehouse truth (Scenario 2)
- Mid-run disruption injection (§6, "the simulation may inject new disruptions")

**Forbidden — not in the spec, do not invent:**
- **PO cancellation / cancellation cost.** The ERP action list (spec §5.9) is:
  mark delayed, create alternate PO, attach notes, update risk status, record
  escalation, store plan. No cancel. Any design needing one is dead.
- Multi-tier BOM networks, carrier/route/hazmat data, customer SLA tiers,
  freight corridors. Wrong domain.

---

## 6. Data model

Use the problem statement's schemas verbatim and its sample records (COMP-104
Motor Driver IC, Pune-Plant-1, SUP-21/42/37/18, PO-7712, PROD-882, PROD-914). Do
not invent a data model; do not rename fields. Extend the same shape for extra
scenarios.

```
GET  /inventory                    GET  /inventory/{component_id}
GET  /purchase-orders              GET  /purchase-orders/{po_id}
GET  /suppliers?component_id=      POST /suppliers/{supplier_id}/message
POST /rfq                          POST /approval/check
POST /erp/update                   GET  /production-schedule
GET  /tracking/{po_id}
```

Dataset: 20–50 components, 10–20 suppliers, 20–40 POs, 5–10 production orders,
10–20 supplier messages.

---

## 7. Shared data structures — proposed shape, not gospel

These prevent four people building four incompatible objects for the same
concept. Adjust field names as the build reveals a better fit, but agree the
change with whoever owns the consuming component before renaming a field.

**DisruptionEvent** — emitted by both COVERAGE and MONITOR (COVERAGE raises
`coverage_breach` / `inventory_mismatch`; MONITOR raises `supplier_delay` from
a poll contradiction). `coverage_breach` is an *effect*; the other five are
*causes*. Do not conflate it with `inventory_mismatch`.
```json
{
  "event_id": "EVT-0001",
  "type": "supplier_delay | inventory_mismatch | demand_spike | expedite_unavailable | priority_change | coverage_breach",
  "component_id": "COMP-104",
  "po_id": "PO-7712",
  "production_order_id": "PROD-882",
  "detail": { "...": "see below — a dict, not free text" },
  "detected_at": "<sim-clock timestamp>",
  "source": "proactive_poll | supplier_message | erp_check"
}
```

`detail` is a **dict**, not free text — this superseded an earlier draft of
this section; `coverage.py` and `monitor.py` both shipped the dict shape
before this line caught up (OPEN_ITEMS.md). Its keys are type-specific, not
one fixed schema across all six `type` values. Every emitter that produces a
human-readable narration puts it under a `"summary"` key first; the remaining
keys are the figures that justify it, so items 6 and 7 can cite a number
without recomputing it. The three types built so far:

- **`supplier_delay`** (MONITOR, `proactive_poll`) — `summary` (string, e.g.
  "PO-7712 is recorded as in_transit, but tracking reads
  label_created_no_pickup..."), `po_status`, `tracking_status`,
  `last_movement`, `exposure_days`, `load_bearing_for` (list of
  `production_order_id`), `detected_by`.
- **`coverage_breach`** (COVERAGE, `erp_check`) — structured only, no
  `summary` key: `days_of_coverage`, `days_of_coverage_on_hand`,
  `days_to_deadline`, `status`, `reason`, `priority`, `component_required`,
  `depends_on_po_ids`.
- **`inventory_mismatch`** (COVERAGE, `erp_check`) — the full
  `InventoryContradiction` record: `component_id`, `erp_current_stock`,
  `warehouse_usable_stock`, `erp_overstatement_units`,
  `erp_snapshot_age_hours`, `coverage_days_if_erp_believed`,
  `coverage_days_on_warehouse_truth`, `decision_changing`, `note` (this one
  carries prose in `note`, not `summary` — a genuine inconsistency, left as
  written rather than silently reconciled; a future pass unifying event
  narration onto `summary` is a real cleanup, not required to unblock 6/7).

`demand_spike`, `expedite_unavailable`, `priority_change` are not built yet —
items 8/9 add their own keys under the same dict-with-a-`summary`-when-narrated
convention.

**Plan** — SOLVER + PLANNER output, consumed by RATCHET and AUDIT.
`reversibility` sits on each ACTION, not one plan-level field (item 5's own
shape change; `safety_stock_draw` resolved to `compensable`, item 5b's build
filled in the `?`). `deadline_feasible` is item 5's own deadline hard-filter
result — `false` means every Pareto candidate missed a high-priority
deadline and this is the best available fallback, RATCHET's exact
"no feasible plan meets a high-priority deadline" signal. `cost_of_inaction`
is a **structured, non-monetary object** — CORRECTED from an earlier
`None`-or-a-rupee-figure design: the real problem statement has no penalty
clause or shutdown cost to compute a money figure from (see
`app/engine/planner.py`'s module docstring for the full search), so this
field never claims a dollar amount that doesn't exist. `cost_increase_vs_
baseline_pct` IS the spec's own metric (§17).
```json
{
  "plan_id": "PLAN-0001",
  "actions": [
    {"type": "purchase_split", "supplier_id": "SUP-42", "qty": 600, "reversibility": "compensable"},
    {"type": "purchase_split", "supplier_id": "SUP-37", "qty": 300, "reversibility": "compensable"},
    {"type": "safety_stock_draw", "days": 4, "justification": "...", "reversibility": "compensable"},
    {"type": "production_reschedule", "production_order_id": "PROD-914", "delay_days": 2, "reversibility": "reversible"}
  ],
  "rejected_alternatives": [
    {"option": "SUP-18 only", "saved": -20751, "regret": "quality_score below COMP-104's required threshold", "reason": "quality_below_threshold"}
  ],
  "deadline_feasible": true,
  "cost_of_inaction": {
    "production_orders_at_risk": [],
    "cost_increase_vs_baseline_pct": 10.32,
    "baseline_total_cost": 112100.0,
    "baseline_note": "Baseline = the delayed PO's own contracted unit_price x quantity_allocated..."
  },
  "total_cost": 123674.0
}
```
`saved` is signed from the chosen plan's point of view: **positive** means
the rejected option would have cost MORE (rejecting it saved money);
**negative** means it would have cost LESS (a real regret, not a saving —
SUP-18 above was cheaper, so rejecting it, correctly, cost something).

**ProvenanceEdge** — one entry in the audit graph (§4, item 7). Built;
real shape below, from `app/audit/provenance.py`. `from`/`to` are
serialization aliases (`from` is a Python keyword — same pattern as
`schemas.py`'s `SupplierMessage.from_`).
```json
{
  "edge_id": "PROV-0009",
  "relation": "Support | Depend-on | Contradict | Invalidate | Trigger | Update",
  "from": "tracking:PO-7712",
  "to": "claim:MSG-0016",
  "produced_by_module": "coverage | monitor | verify | solver | planner | ratchet",
  "input_record_ids": ["PO-7712", "MSG-0016", "SUP-21"],
  "model_version": "gemini-<version> | deterministic",
  "timestamp": "<sim-clock timestamp>",
  "note": "claim was 'dispatched', but tracking status is 'label_created_no_pickup' — the shipment has not moved."
}
```

**There is no severity, weight, or confidence field on an edge, and there
must never be one.** An edge exists or it doesn't (AGENTS.md rule 7 — a
"0.7-strength Support edge" is an invented metric displayed as a finding).
What an edge carries instead is where it came from: the module, the actual
record IDs consumed, and `model_version`.

`model_version` is read off the records, never chosen by the graph builder.
VERIFY edges carry `ClaimVerification.claim.model_version` (item 3's real
field — the one that legitimately reads `gemini-<version>`). **RATCHET edges
are always `"deterministic"`, NOT `DecisionBrief.model_version`** — the brief's
field describes its *narration*, which may be LLM-rephrased, while the
decision itself comes from `_evaluate_triggers`, pure Python an LLM cannot
reach (rules 1 and 3). Tagging a `Trigger` edge with the narration's model
version would assert in the audit trail that an LLM produced the escalation.
The narration's provenance is recorded separately on
`DecisionRecord.narration_model_version`.

**ProvenanceGraph** — item 7's ONE object: the audit trail and the
assumption ledger together, per §4 ("as ONE object"), plus the per-decision
reproducibility log and tool-call accounting.
```json
{
  "built_at": "<sim-clock timestamp>",
  "edges": ["<ProvenanceEdge>, ..."],
  "assumptions": [
    {"assumption_id": "ASSUM-0004", "subject_node": "brief:PLAN-0001",
     "statement": "<verbatim from source_field>", "source_module": "ratchet",
     "source_field": "DecisionBrief.falsification_line",
     "model_version": "deterministic", "timestamp": "<sim-clock timestamp>"}
  ],
  "decisions": [
    {"decision_id": "DEC-0004", "decision_node": "brief:PLAN-0001", "module": "ratchet",
     "outcome": "execute", "input_record_ids": ["PLAN-0001", "COMP-104"],
     "model_version": "deterministic", "narration_model_version": "gemini-<version>",
     "timestamp": "<sim-clock timestamp>"}
  ],
  "regret_ledger": ["<planner's own RejectedAlternative objects, by reference>"],
  "cost_of_inaction": "<planner's own CostOfInaction object, by reference>",
  "tool_calls": {
    "monitor_polls_made": 2, "monitor_polls_available": 15,
    "verify_probes_made": 0, "verify_probes_reused_from_monitor": 1,
    "solver_quotes_requested": 3, "solver_quotes_reused": 0,
    "ratchet_approval_checks_made": 1,
    "total_calls_made": 6, "calls_avoided_by_gating": 14,
    "notes": ["<one plain-language line per module>"]
  }
}
```
`regret_ledger` and `cost_of_inaction` hold PLANNER's **own instances**, not
restatements of their figures — `graph.regret_ledger[i] is
plan.rejected_alternatives[i]`, asserted by test. Nothing in the graph reads
`saved` or `cost_increase_vs_baseline_pct` and writes a second copy, so a
discrepancy between the graph and the plan is structurally impossible rather
than merely unlikely. `tool_calls` deliberately computes no ratio or
efficiency score — item 10 owns that.

**ReliabilityRecord** — VERIFY's persistent memory per supplier
```json
{
  "supplier_id": "SUP-21",
  "score": 0.62,
  "last_updated": "<sim-clock timestamp>",
  "last_change_reason": "tracking_contradiction | confirmed_delivery"
}
```

---

## 8. Simulator invariants — do not break these

Both were found by smoke-testing item 2 and both are load-bearing for the demo.
Keep the regression tests that guard them.

**`build_store()` must return a clean slate.** Anchor records are deep-copied
per build. If Stores share module-level anchor objects, marking PO-7712 delayed
in one run leaks into the next — which means **the judge panel (Beat 5) cannot
reset between injections.** This is a demo-killer, not a test nicety.

**Anchor IDs are reserved.** `_assert_anchor_ids_reserved()` runs from
`build_store()` and enforces all eight spec IDs (COMP-104, SUP-21/42/37/18,
PO-7712, PROD-882, PROD-914). Generated ranges must never collide: `Store` keys
by ID, so a collision silently *overwrites* an anchor while collection counts
still look correct. `PROD-{900+i}` over `range(5)` sits ten below PROD-914 —
one edit from breaking. The assertion raises explicitly so it survives `python -O`.

**Seed collection sizes stay inside spec §16 ranges** after any seed edit:
components 20–50, suppliers 10–20, POs 20–40, production orders 5–10, messages
10–20. Fixing one collection has twice now pushed another out of range.

---

## 9. Repository layout

```
trace/
├── AGENTS.md
├── CLAUDE.md
├── PROJECT.md
├── ARCHITECTURE.md
├── BRAND.md
├── .env.example
├── .gitignore
├── requirements.txt
├── scripts/
│   └── task_zero_gemini_check.py
├── app/
│   ├── main.py                 # FastAPI app; mounts routes + static/
│   ├── config.py                # env vars, TRACE_LLM_ENABLED flag
│   ├── environment/              # item 1 — the simulator
│   │   ├── schemas.py            # spec's data shapes, verbatim
│   │   ├── seed_data.py          # sample records + generated dataset
│   │   ├── clock.py              # simulated time
│   │   └── routes.py             # the §6 REST surface
│   ├── engine/
│   │   ├── coverage.py           # item 2 — days-of-coverage, mismatch detection
│   │   ├── monitor.py            # item 2b — gated proactive polling
│   │   ├── verify.py             # item 3 — claim verification, reliability EWMA
│   │   ├── solver.py             # item 4 — hard filter + Pareto
│   │   ├── planner.py            # item 5 / 5b — multi-action plans, safety stock
│   │   ├── ratchet.py            # item 6 — escalation rules, decision brief
│   │   ├── staleness.py          # item 8 — staleness detector, re-entry
│   │   └── contingency.py        # item 9 — trigger / fallback plans
│   ├── audit/
│   │   └── provenance.py         # item 7 — the provenance graph
│   ├── llm/
│   │   └── gemini_client.py      # thin wrapper, only called when TRACE_LLM_ENABLED
│   └── api/
│       └── routes.py             # ✅ ORCHESTRATOR — the assembled pipeline
│                                 #    + THE ERP-WRITE BOUNDARY (rule 5)
├── static/
│   └── index.html                # items 11/12 — coverage board + judge panel
└── tests/
    ├── conftest.py               # LLM-off default + outbound-network kill switch
    ├── test_coverage.py
    ├── test_monitor.py
    ├── test_verify.py
    ├── test_solver.py
    ├── test_planner.py
    ├── test_ratchet.py
    ├── test_provenance.py
    └── test_orchestrator.py      # the ERP-write boundary lives or dies here
```

**`app/api/routes.py` is the only caller of `POST /erp/update` in this
codebase** — asserted by
`tests/test_orchestrator.py::test_the_orchestrator_is_the_only_caller_of_erp_update`,
which greps every engine module. One call site, behind one guard
(`_write_erp_once`), keyed on `plan_id`. See §12.

**`tests/conftest.py` blocks all non-loopback outbound network calls for the
whole suite.** Three separate times a test enabled the LLM and mocked only
one of the two entry points, leaking a live Gemini call and free-tier quota
while still passing. The guard turns a mock gap into a loud failure. Loopback
is deliberately allowed: on Windows, asyncio's ProactorEventLoop builds its
self-pipe with `socket.socketpair()`, and blocking that breaks
`fastapi.testclient.TestClient` outright.

---

## 10. Hidden-test coverage

| Hidden test | Handled by |
|---|---|
| Supplier delays after confirming | 3 — claim verification + reliability memory |
| Claims dispatch, tracking contradicts | 3 — provenance-based downgrade |
| Cheapest supplier fails quality | 4 — per-component quality-threshold hard filter before RFQ |
| High-reliability supplier lacks quantity | 5 — order splitting |
| Low-reliability supplier is fastest | 4 — Pareto surfaces the tradeoff |
| Purchase exceeds approval limit | 6 — hard ratchet + decision brief |
| ERP inventory overstates real stock | 2 — mismatch detection, logged as contradiction |
| Sudden demand spike | 8 — staleness detector re-triggers coverage |
| Expedited delivery unavailable | 9 — contingency trigger fires fallback |
| Production priority changes mid-run | 5 — reschedule is in the action space |

Eight of ten depend on items 5, 8, 9. If those slip, coverage drops to six.

---

## 11. Multi-baseline comparison harness (item 13)

The strongest differentiator available, because it is copy-resistant
*structurally* rather than conceptually: building baseline variants requires the
engine to already be modular and working. A team at hour 15 with a monolithic
agent physically cannot retrofit this.

Run all four variants on the **same judge-selected disruption**:

| Variant | Implementation | Expected to fail on |
|---|---|---|
| Static workflow | Fixed stage sequence, staleness detector off | Mid-run disruptions |
| Cheapest-always | Skip VERIFY, skip reschedule, min price | Quality + supplier claims |
| Retry-only | Retries failed calls, never re-enters the loop | Expedite-unavailable |
| **TRACE** | Full pipeline | — |

These are **the same engine with stages disabled**, not three new agents. Build
them as config flags on the existing pipeline, not as separate code paths.

**Report per variant:** days of coverage protected, total spend, hidden tests
passed, tool calls used, and **silent-failure rate**.

A *silent failure* is the agent reporting success when the outcome was bad — it
completes, writes to ERP, and coverage was actually breached. Measure it by
comparing the agent's claimed outcome against simulator ground truth. Nobody
else will measure this, it costs ~30 minutes once the harness exists, and "our
silent-failure rate is zero, here's the baseline's" is a line a judge repeats.

**Free bonus — component ablation.** The same harness runs TRACE with
claim-verification disabled: it gets fooled by SUP-21 and the line stops. That
proves each component earns its place, which is a far better answer than "we
built it because the spec said so."

---

## 12. The ERP-write boundary (orchestrator, item 7b)

AGENTS.md rule 5: *"Exactly one action is irreversible: `POST /erp/update`."*
That is a claim about the whole system, so it needs one enforcement point,
not a convention each module is trusted to follow.

**One call site.** `app/api/routes.py::_write_erp_once` is the only place in
this codebase that calls `store.update_erp`. Every engine module is asserted
free of it by test. RATCHET decides; it does not write (its own §3 stage is
drawn separately from `ERP WRITE` for exactly this reason).

**Three guarantees, each with a test that fails if it breaks:**

| Guarantee | Test |
|---|---|
| Write fires **iff** the verdict is `execute` | `test_erp_write_fires_when_the_verdict_is_execute`, `test_escalate_produces_zero_erp_writes_before_approval` |
| `escalate` writes **nothing** and waits for a human | `test_escalate_produces_zero_erp_writes_before_approval`, `test_escalate_with_no_feasible_plan_also_writes_nothing` |
| Idempotent per `plan_id`, however it is reached | `test_firing_the_same_disruption_twice_produces_exactly_one_erp_write`, `test_approving_twice_produces_exactly_one_erp_write`, `test_approval_after_an_execute_does_not_write_a_second_time` |

Not even `record_escalation` is written on the escalate path — it is one of
§5.9's six actions and therefore itself an irreversible write. A rejected
plan is closed on the run record, with no ERP write at all.

**Two idempotency registries, both needed.** `_ERP_WRITES_BY_PLAN` keys on
`plan_id` and stops a second write for one plan (covers double approval).
`_RUNS_BY_COMPONENT` keys on `component_id` and stops the pipeline minting a
*second* `plan_id` for the same unresolved disruption — without it, "fire the
same disruption twice" produces PLAN-0001 and PLAN-0002, and the per-plan
guard correctly lets both through. **Item 8 (staleness) is what will
legitimately invalidate the run cache**; `reset_orchestrator_state()` is the
hook until then, and the judge panel (item 11) needs it between injections
alongside `build_store()`'s clean slate.

**Stage order.** The orchestrator runs COVERAGE before MONITOR — MONITOR is
drawn first in §3 because it *initiates* the loop, but its gate is
`coverage.polling_targets()`, so it cannot run first. TOOL GATE is not a
separate call: it is already embodied in MONITOR's load-bearing gate and
VERIFY's reuse of MONITOR's tracking read. `PipelineRun.stages` records the
sequence actually walked.

---

## 13. Staleness, re-entry, and why rule 5 tagging is load-bearing (item 8)

`app/engine/staleness.py` answers two questions; `app/api/routes.py` executes
the answer. The module decides *what is stale* and *where to re-enter*; the
orchestrator only sequences.

**A diff against current state, never a timer.** `capture_preconditions`
records the exact field values a plan rests on when it is built;
`detect_staleness` re-reads those same fields and reports what moved. Even
quote expiry — the one check that touches the clock — compares against that
quote's own `quote_issued_at + quote_valid_hours` (§5.7), a fact about the
quote, not a replanning interval. A test strips docstrings and asserts no
interval/TTL vocabulary appears in executable code.

**Earliest-conflict mapping.** Each changed fact maps to the earliest stage
it invalidates; `reentry_stage` is the minimum:

| Fact that moved | Earliest invalid stage |
|---|---|
| inventory stock / usage / safety levels | COVERAGE |
| production deadline / priority / demand | COVERAGE |
| purchase-order status (dependable inbound set) | COVERAGE |
| tracking status of a polled PO | MONITOR |
| a new supplier claim on a load-bearing PO | VERIFY |
| supplier price / lead time / availability / reliability / quality / certs | SOLVER |
| a quote passing `quote_valid_hours` | SOLVER |
| the approval threshold | RATCHET |

Two look interchangeable and are not. **Tracking change → MONITOR**, because
MONITOR is what physically reads tracking and VERIFY reuses that read; re-
entering at VERIFY would hand it a stale value. **A new claim → VERIFY**,
because MONITOR's read is still valid and only the assertion being compared
changed.

**Rule 5 tagging is not decoration — it prevents a real corruption.**

| Stage | Tag | Why |
|---|---|---|
| COVERAGE / MONITOR / SOLVER / PLAN / RATCHET | `idempotent` | pure reads and computation; SOLVER and MONITOR spend tool calls but change no decision state |
| **VERIFY** | **`compensable`** | it MUTATES `reliability_score` via the exponentially weighted update |
| ERP_WRITE | `irreversible` | rule 5's one — not a re-enterable stage at all |

VERIFY is genuinely not idempotent: re-running it on identical, unchanged
evidence re-applies the update — **measured, not theorised: SUP-21 goes
0.75 → 0.45 → 0.27 → 0.162 across three passes.** A naive "re-enter at
COVERAGE and re-run everything below" rollback would therefore silently
destroy supplier reliability scores every time an unrelated stock level
moved. `stages_to_rerun` prevents it: a non-idempotent stage is re-run ONLY
when a finding names it directly. After a COVERAGE-level re-entry the prior
`VerificationReport` is reused — its conclusion is a fact about a tracking
record that has not changed; what would be false is applying the penalty
twice for the same evidence.

**You cannot roll back past an irreversible write.** A plan already executed
and then found stale is not undone. The replan produces a **superseding**
plan and the POs the first one created stand (§5 excludes PO cancellation).
Those POs are `pending` — a dependable inbound status — so the next coverage
pass credits them automatically and the superseding plan accounts for stock
already on order rather than double-buying.

**Bounded, per rule 6.** `MAX_REENTRY_PASSES` caps replanning per call. It is
a termination guarantee, not a threshold on anything measured. After each
replan the NEW plan's preconditions are captured and re-checked — a second
pass is not trusted for being second. Still stale at the bound is reported
(`post_replan_verified=False` plus residual findings), not looped on: an
environment moving faster than planning converges is itself the finding.
