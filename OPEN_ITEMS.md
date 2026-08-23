# OPEN_ITEMS.md

Live tracker. Delete lines as they close. Not a design doc — see ARCHITECTURE.md.

## Signed off — Item 13 (Multi-baseline comparison harness) built and verified

- [x] **Item 13 shipped.** Multi-baseline comparison harness (`app/engine/baseline.py` + `POST /baseline/compare/{scenario_name}`) built as config flags on the existing pipeline (`variant` parameter in `run_pipeline`).
  - Core variants built: `static_workflow`, `cheapest_always`, `retry_only`, `trace` (control).
  - Bonus ablation mode built: `claim_ablation`.
  - Reports per variant: `days_of_coverage_protected`, `total_spend`, `hidden_tests_passed`, `tool_calls` (item 10 ToolAuditReport), `silent_failure` (ground-truth evaluation), `summary_sentence`.
  - Smoke test: PO-7712 disruption fired through all variants, proving `static_workflow`, `cheapest_always`, `retry_only`, and `claim_ablation` all report a silent failure (`silent_failure=True`) while `trace` protects 5.56 days of coverage with 0 silent failures (`silent_failure=False`).
  - AST checks passed: 0 invented metrics, 0 banned terms, 0 LLM in variant/comparison logic.
  - Both `TRACE_LLM_ENABLED=true` and `TRACE_LLM_ENABLED=false` modes pass 100% of 373 tests.
  - **COORDINATION NOTE:** This session (Item 13 backend session) touched `app/api/routes.py` LAST to add `variant` parameter to `run_pipeline` and expose `POST /baseline/compare/{scenario_name}`. If another session edits `routes.py`, pull and merge carefully before committing.

## Needs a call — Plan shape: reversibility moved per-action

- [ ] ARCHITECTURE.md §7's `Plan` shape previously had ONE `reversibility`
      field for the whole plan. Item 5 tags reversibility on EACH action
      instead (`purchase_split` -> `compensable`, `production_reschedule`
      -> `reversible`, `safety_stock_draw` -> `compensable` — resolved,
      item 5b's own "?" is now filled in) — see `app/engine/planner.py`'s
      module docstring for the reasoning on all three. ARCHITECTURE.md §7 is
      updated to match. Item 6 (RATCHET) is the consuming component §7 says
      should agree a shape change; confirm this works for it, or say what
      needs to change.

## Owed by whoever touches item 5's allocation logic again — a real behavior change

- [ ] **`allocate_stock`'s default on-hand pool changed while building item
      5b.** It used to allocate from the FULL `usable_stock` (silently
      dipping into `safety_stock` with no authorization at all, discovered
      while building 5b — this made 5b's gate decorative unless fixed).
      `run_planner` now allocates from `usable_stock - safety_stock` by
      default; the reserve is only spent through `_safety_stock_decision`'s
      explicit, justified mechanism. Confirmed via
      `tests/test_planner.py::test_smoke_against_the_real_current_state`
      that this does NOT change the real current-dataset outcome (both
      orders still land on time, item 5b's trigger correctly does not fire)
      — but this IS a disclosed behavior change from item 5's originally
      shipped code, not purely additive. If anything downstream assumed the
      old (unreserved) behavior, it needs to be re-checked.
- [ ] **A real bug caught while testing item 5b, fixed before it shipped:**
      the first version of the "does this draw hurt another order" check
      applied the on-hand-only reclassification to EVERY order sharing the
      component, including the one the draw exists to help — which
      penalized that order for benefiting from its own rescue (the on-hand-
      only lens ignores incoming supply entirely, so it looked like the
      beneficiary's situation got worse when it actually got dramatically
      better). Fixed by excluding "bystander" orders only — those receiving
      ZERO of the chosen combination's incoming supply under the baseline
      allocation. Recorded here in case a similar mistake gets reintroduced
      by a future change to `allocate_stock` or `_safety_stock_decision`.

## Needs a call — selection rule puts reliability ahead of lead time (PARTIALLY resolved)

- [ ] `planner.py`'s `SELECTION_RULE` ranks reliability_score above
      lead_time_days (see the module docstring's full reasoning: coverage's
      own "gap is the exposure to supplier claims" framing, and BRAND.md's
      pitch that a claim gets checked "before it's trusted" — trusted enough
      to change what gets picked, not just logged). Concretely, against the
      real current dataset (item 4's post-verify Pareto set for COMP-104),
      this picks the SUP-37+SUP-42 split over SUP-21 alone, even though
      SUP-21 is 3 days faster and ~₹13,500 cheaper — because item 3 already
      downgraded SUP-21 to 0.45 reliability after the PO-7712 contradiction.
      **This is a genuine design call, not a spec-given rule.**
      **PARTIALLY RESOLVED by item 5's correction [3]:** the worst
      CONSEQUENCE of this risk — a reliability-favored plan silently missing
      a high-priority deadline — can no longer happen, because deadline
      feasibility is now a hard filter applied BEFORE this ranking runs (see
      the "RESOLVED — deadline-miss gap" entry above). What remains open:
      AMONG feasible candidates (all meeting every high-priority deadline),
      a trivial reliability difference (0.81 vs 0.80) still outranks a huge
      lead-time difference (6 days vs 60) just as decisively as the real
      0.81-vs-0.45 gap did — that part of the tier's behavior is unchanged
      and still a live design question, just no longer able to cause a
      deadline miss. A more nuanced version — gating on the DISCRETE fact
      "was this specific claim contradicted" (from
      `ClaimVerification.contradicted`) rather than the continuous
      `reliability_score` — would still be worth considering, but needs
      `planner.py` to consume a `VerificationReport` too, a real interface
      change not made here. Flagging for whoever revisits this, not fixed
      unilaterally.
- [ ] **Smoke-tested against the real current state, and it does NOT match
      PROJECT.md §4 Beat 4's promised "delay PROD-914 by two days."** Against
      the actual post-verify Pareto set, the chosen split (SUP-37+SUP-42)
      allocates the faster-arriving SUP-42 portion to PROD-882 (high
      priority) and the remainder to PROD-914 — BOTH orders land before
      their own deadlines under this allocation, so `_reschedule_actions`
      produces nothing. This was checked, not forced: see
      `tests/test_planner.py::test_smoke_against_the_real_current_state` for
      the full real numbers. If the demo needs "delay PROD-914 by two days"
      specifically, that requires either different seed numbers (already
      owed to person A) or a different selection rule than the one above —
      not a change to `allocate_stock`'s allocation logic, which is correct
      given whatever it's handed.

## Owed by whoever builds item 5 (reads item 4's output)

- [ ] **`solver.py` deliberately does not compute a dollar "saved" figure**
      on rejected alternatives (`Rejection` has no `saved` field). Computing
      "saved" requires knowing which option gets executed — a decision
      `solver.py` doesn't make (it produces the Pareto SET, not a single
      pick). Item 5/6 should compute `saved` against whatever it selects,
      reading the comparable figures already on each `Rejection`
      (`estimated_unit_price`, `estimated_total_price`, `lead_time_days`,
      `reliability_score`, `quality_score`) rather than re-fetching or
      re-deriving them.
- [ ] **Combinations cap at 2 suppliers** (`MAX_COMBINATION_SIZE = 2`,
      ARCHITECTURE.md §1's own justification: "~120 combinations for
      ≤2-of-14 suppliers"). A shortfall too large for any single supplier or
      pair to cover (rare given the seeded dataset's availability figures,
      but possible after enough injected disruptions) will return an empty
      `pareto_set` rather than a 3-way split. Item 5 should treat an empty
      Pareto set as its own signal (likely feeding RATCHET's escalation
      path, item 6) rather than assuming a plan always exists.
- [ ] **Split allocation within a pair is cheapest-first, not optimized.**
      `_allocate_pair` fills the cheaper of two suppliers first, up to its
      availability, then the remainder to the other. This produces ONE
      concrete, evaluable combination per pair — not a claim that it's the
      only or best split of that pair. If item 5 wants a different
      allocation within the same two suppliers (e.g. balancing lead time
      instead of price), that's a new allocation rule, not a bug in this one.

## Shipped — Items 11 & 12 (Coverage Board + Judge Panel UI)

- **Single deliverable `static/index.html`** holding both the **Coverage Board** and **Judge Panel** (vanilla HTML/CSS/JS, no build step, no framework).
- **Coverage Board**: Live table polling `/production-schedule` and `/inventory`, rendering both `days_of_coverage` and `days_of_coverage_on_hand` with Exposure Gap highlighting and status badges (`healthy`, `at_risk`, `critical`, `thin coverage`).
- **Judge Panel**: Interactive controls for 9 hidden-test disruption scenarios (`supplier_delay`, `quality_fail`, `insufficient_qty`, `low_reliability_fastest`, `exceeds_approval`, `stale_erp`, `demand_spike`, `expedite_unavailable`, `priority_change`). Supports sequential dual-injection without page reload.
- **RESET Button**: Calls `POST /environment/reset` (`build_store()` + `reset_orchestrator_state()`).
- **Decision Brief & Audit Output**: Displays `execute`/`escalate` verdict, total spend vs approval threshold, falsification line, plain-language narration, human approval actions (`POST /agent/approval/{plan_id}`), recovery plan actions with reversibility tags (`compensable`, `reversible`, `irreversible`), rejected alternatives with quantified regret, cost of inaction, tool audit gating summary, and provenance graph trail.
- **Backend endpoints**: `POST /environment/reset` and `POST /environment/inject/{scenario_name}` added to `app/environment/routes.py`. Root route `/` serves `static/index.html`.

## Signed off — no action, recorded so it isn't re-litigated

- **`model_version` now distinguishes the LLM and deterministic paths in
  the recorded output**, not just internally. `parsed_by` ("llm"/
  "deterministic") already flipped correctly on fallback, but nothing
  carried the literal model-version string §7 wants, and
  `gemini_client.py`'s model constant was private — so item 7 would have had
  no clean source to read a real `"gemini-3.6-flash"` from without
  duplicating it. Fixed live, not hypothetically: while verifying this, the
  Gemini free-tier quota (20 req/day) was already exhausted from earlier
  testing — `parse_supplier_claim` raised `429 RESOURCE_EXHAUSTED`, the
  fallback fired, and `model_version` correctly recorded `"deterministic"`
  rather than silently claiming `"llm"`. Logged above under "Owed by
  whoever builds item 7."
- **"Dependable inbound" stays reliability-independent — resolved, not a
  gap.** `coverage.py`'s `dependable_inbound()` remains a pure PO-status
  filter; it will never read `reliability_score`, and does not read VERIFY's
  per-PO contradiction findings either. Coupling reliability in would break
  "the gap between the two coverage metrics is the exposure to supplier
  claims," which is the project's own stated thesis (BRAND.md §3). Full
  reasoning in `app/engine/verify.py`'s module docstring; `coverage.py`'s
  `DEPENDABLE_PO_STATUSES` comment now points to it instead of promising a
  tightening that was never going to happen. A related but genuinely
  different question is logged above under "Owed by whoever builds item 8."
- **`PROJECT.md` renamed back.** It had been saved to disk as
  `PROJECT (1).md` (a download-artifact name, not a deliberate rename).
  AGENTS.md and CLAUDE.md both instruct readers to read `PROJECT.md`
  verbatim; those references now resolve again. This repo has no `.git`, so
  there was no `git mv` / commit to make — flagging in case that surprises
  whoever expected one.
- **`DisruptionEvent.detail` is a dict, not free text.** ARCHITECTURE.md §7
  previously documented it as free text while `coverage.py` and `monitor.py`
  had already shipped dict-shaped `detail`. §7 now documents the real
  per-type key sets (`supplier_delay`, `coverage_breach`,
  `inventory_mismatch`), with `"summary"` as the first key wherever an emitter
  narrates in prose. Code was not changed — only the doc was brought in line
  with what already shipped. One loose end noted in §7 itself, not repeated
  here: `inventory_mismatch` narrates via `note`, not `summary` — a real
  inconsistency, left as is, not blocking items 6/7.
- `polling_targets()` gates on **load-bearing** POs, not thin-coverage ones.
  ARCHITECTURE.md §4 item 2b updated to match. The thin gate misses PO-7712 at
  Beat 1, which is the exact moment the proactive poll exists to beat.
- `DisruptionEvent` gained `coverage_breach`, `production_order_id`, `detail`.
  Additive only. `coverage_breach` is an effect; the other five are causes.
- **Task zero passes.** Ran 2026-08-22, exit 0, `gemini-3.6-flash` returned a
  well-formed `report_supplier_claim` call: `po_id=PO-7712`,
  `claim_status=dispatched`, `claimed_delay_days=3`. Note it extracted *both*
  the dispatch claim and the 3-day delay from one message — the contradiction
  item 3 needs. The script writes no artifact, so there was no way to tell a
  prior run from no run; it was rerun rather than assumed. If you need to prove
  it again, rerun it — don't infer from this line.
- Two coverage metrics, both reported. Standard MRP practice credits scheduled
  receipts; the on-hand floor is the verified figure. **Frame the gap as the
  exposure to supplier claims** — that's the project thesis in the metric.
