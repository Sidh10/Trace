# OPEN_ITEMS.md

Live tracker. Delete lines as they close. Not a design doc — see ARCHITECTURE.md.

## RESOLVED — orchestrator built; the ERP-write boundary is enforced

`app/api/routes.py` is the single call site for `POST /erp/update` in this
codebase (asserted by a test that greps every engine module). The boundary
and its three guarantees are documented in ARCHITECTURE.md §12.

The open question this entry asked — *which ERP action follows each
`DecisionBrief.decision`* — is answered as: **`store_plan` on `execute`, and
nothing at all on `escalate`.** See the two sign-off items below for the
parts of that answer that are a judgement call rather than a given.

## RESOLVED — `execute` now creates the POs

- Signed off and built: `_write_erp_once` emits `store_plan` then one
  `create_alternate_po` per `purchase_split`, carrying that split's own
  supplier, quantity, unit_price, and `expected_delivery` = sim-clock now +
  that supplier's `lead_time_days`. No approval ceiling (RATCHET already
  authorised the total). PO ids come from `Store.update_erp`'s existing
  anchor-safe range — this module passes no `po_id` and contains no id
  arithmetic, so ARCHITECTURE.md §8's collision bug class cannot recur.

## NEEDS SIGN-OFF — orchestrator decisions I did not make unilaterally

- [ ] **The approval endpoint is `POST /agent/approval/{plan_id}`, not the
      bare `POST /approval/{plan_id}` that was requested.** The environment
      already serves the problem statement's own `POST /approval/check`
      (§5.8); a bare `/approval/{plan_id}` sits in the same path space as a
      spec endpoint and resolves by router registration order — a
      route-ordering accident waiting to happen, and confusing to read.
      Namespaced under `/agent/` alongside the other orchestrator endpoints.
      Say if the bare path is required and I will add it as an alias.
- [x] ~~Run-cache idempotency keyed on `component_id` returns a stale run for
      a genuinely new disruption~~ — **FIXED by item 8.** The cache is now
      returned only when `detect_staleness` confirms the plan's preconditions
      still hold; otherwise the pipeline re-enters at the earliest
      invalidated stage. Both original guarantees survive: an UNCHANGED
      component still returns the cached run (so "same disruption twice = one
      write set" still holds), and a CHANGED one replans.

## RESOLVED — a real integration defect found while assembling the pipeline

- `app/environment/routes.py` bound `STATE` by name at import
  (`from app.environment.seed_data import STATE`), so the environment router
  and the orchestrator could serve **different `Store` instances**: a
  disruption injected through the environment endpoints was invisible to the
  agent planning against the other one. Caught by an end-to-end HTTP smoke
  test whose provenance graph came back 14 edges instead of 17, verifying
  against the wrong inbox message. Both now resolve `seed_data.STATE` at call
  time. **This would have broken the judge panel (item 11) specifically** —
  it injects on one side and reads results on the other. Guarded by
  `tests/test_orchestrator.py::test_environment_and_orchestrator_share_one_store`.

## NEEDS SIGN-OFF — found while building item 8

- [ ] **VERIFY is not idempotent, and that is now load-bearing.** Re-running
      `run_verification_cycle` on identical unchanged evidence re-applies the
      exponentially weighted reliability update: SUP-21 goes 0.75 -> 0.45 ->
      0.27 -> 0.162 across three passes. Item 8 works around this rather than
      fixing it — `staleness.STAGE_REVERSIBILITY` tags VERIFY `compensable`
      and the rollback rule refuses to re-run it unless a finding names it
      directly (ARCHITECTURE.md §13). **The deeper fix would be to make the
      reliability update idempotent on its evidence** — keyed on
      (supplier_id, po_id, message_id, tracking_status), so re-observing the
      SAME contradiction does not penalise twice. That is arguably what rule
      4 already implies (one tracking record, one downgrade), but it changes
      shipped item-3 behaviour and its tests (`test_verify.py::
      test_repeated_cycles_on_an_unchanged_store_agree` currently ASSERTS the
      double application), so it was not done unilaterally. Say the word and
      it is a contained change to `verify.py` plus that one test.

## RESOLVED — staleness now cites the exact quotes the solve used

- `SolverResult` now carries `quotes_used: dict[str, RFQQuote]`, and
  `capture_preconditions(..., solver_result=...)` records quote expiries from
  those exact quote objects. The previous inferential path — taking the latest
  matching quote from `store.rfq_log` — is gone from staleness capture.

## RESOLVED — item 8's staleness trigger question

- The question logged here ("when VERIFY confirms a contradiction, should
  COVERAGE stop crediting that PO?") is answered by the mechanism rather
  than by a special case: `staleness._check_po_states` maps a PO status
  change to COVERAGE, and `_check_tracking` maps a tracking change to
  MONITOR. So when something actually changes the PO's standing, coverage is
  recomputed through normal re-entry. What the orchestrator still does NOT do
  is mark the PO delayed itself — that is an ERP write, and it must not
  happen on the escalate path. See ARCHITECTURE.md §13.

## RESOLVED — deadline-miss gap, fixed by item 5's correction [3] + item 6

The gap this section used to describe (`SELECTION_RULE` could pick a
reliability-favored plan that silently misses a high-priority deadline,
with nothing downstream catching it) is fixed two ways, authoritative
correction supplied 2026-08-23:
- `planner.py`'s selection now runs deadline feasibility as a HARD FILTER
  before ranking (`_partition_by_deadline_feasibility` /
  `_is_deadline_feasible`) — a combination missing a high-priority
  deadline is dropped (`RejectedAlternative.reason="deadline_infeasible"`)
  BEFORE `SELECTION_RULE`'s reliability/lead-time/price tiers ever compare
  it against anything. `Plan.deadline_feasible=False` when even the best
  available option still misses, so a plan is always returned (never
  silently `None`) with an explicit flag.
- `ratchet.py` (item 6) reads that flag directly as its
  `no_feasible_deadline_plan` hard trigger and escalates — AGENTS.md
  rule 3's "no supplier meets deadline" is now actually implemented.
- The adversarial test that exposed this
  (`tests/test_planner.py::test_ADVERSARIAL_higher_reliability_combination_missing_the_deadline_is_now_filtered_not_chosen`)
  now asserts the FIXED behavior: SUP-RISKY (meets the deadline) is chosen
  over SUP-RELIABLE (misses it by 15 days), even though SUP-RELIABLE would
  win `SELECTION_RULE`'s own tiers outright.

## RESOLVED — cost_of_inaction, corrected shape (was BLOCKING)

The earlier "keep searching for a money figure" framing was itself the
mistake — authoritative correction (2026-08-23): the real problem statement
has no penalty clause, shutdown cost, or lost-revenue figure either.
`Plan.cost_of_inaction` is now `CostOfInaction`, a structured, non-monetary
object (`production_orders_at_risk`, `cost_increase_vs_baseline_pct` — the
spec's own §17 metric, `baseline_total_cost`), never bare `None` at the
`Plan` level. Full derivation in `app/engine/planner.py`'s module docstring.
`ratchet.py` (item 6) surfaces it in the decision brief by reading
`plan.cost_of_inaction` directly, never recomputing.

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

## RESOLVED — "certified" interpretation, corrected against the real spec

- Previous session's "certified = holds at least one certification" reading
  was WRONG, per the actual problem statement (authoritative, supplied
  2026-08-23): certification requirements are PER-COMPONENT ("some
  components require certified suppliers," not all), and Scenario 4's model
  answer rejects SUP-18 on `quality_score`, not certifications.
  `app/engine/solver.py`'s hard filter now runs two independent checks —
  `_meets_certification_requirement` (component's own
  `required_certifications`, trivially satisfied when empty) and
  `_meets_quality_requirement` (component's own `required_quality_score`) —
  both reading additive fields on `InventoryRecord` (schemas.py). COMP-104
  seeds `required_certifications=["ISO-9001"]`,
  `required_quality_score=0.85`; SUP-18 now holds ISO-9001 (updated from
  `[]`) specifically so certification passes and quality (0.71 < 0.85) is
  the sole, demonstrated reason it's dropped. `DropReason` gained
  `quality_below_threshold` as its own value, distinct from `uncertified`.

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

## Owed by whoever builds item 10 (reads item 7's output)

- [ ] **`ProvenanceGraph.tool_calls` is emitted and ready — it computes no
      ratio on purpose.** `ToolCallSummary` totals only counters the upstream
      reports already maintain (`MonitorReport.polls_made/polls_available`,
      `VerificationReport.probes_made/probes_reused_from_monitor`,
      `SolverResult.quotes_requested/quotes_reused`,
      `DecisionBrief.approval_checks_made`), plus `total_calls_made`,
      `calls_avoided_by_gating`, and a plain-language `notes` line per
      module. **No efficiency ratio, percentage, or score is computed** —
      that is item 10's call to make, and inventing one in item 7 would be a
      metric this module has no mandate for (AGENTS.md rule 7). A test
      asserts no field on `ToolCallSummary` contains "ratio"/"efficiency"/
      "pct", so adding one is a deliberate act, not an accident.
- [ ] Item 10's brief also mentions "tool-call **precondition** logging."
      Item 7 records the COUNTS and the reason-per-module, but the
      per-call precondition ("was this specific call actually needed at the
      moment it was made") lives on the upstream decision records —
      `PollDecision.reason` ("load_bearing" / "not_load_bearing"),
      `VerificationSkip.reason`, `ClaimVerification.probe_source`. Those are
      already populated; item 10 should read them rather than adding a
      second logging path.

## RESOLVED — item 7's model_version wiring

- `ProvenanceEdge.model_version` reads `ClaimVerification.claim.model_version`
  for VERIFY edges (item 3's real field, never re-derived from `parsed_by`,
  never a hardcoded model string). One correction made while building it,
  worth knowing: **RATCHET edges are tagged `"deterministic"`, NOT
  `DecisionBrief.model_version`.** The brief's field describes its
  *narration*, which may be LLM-rephrased; the decision itself comes from
  `_evaluate_triggers`, pure Python an LLM cannot reach. Tagging a `Trigger`
  edge with the narration's model version would assert in the audit trail
  that an LLM produced the escalation decision — the exact claim AGENTS.md
  rules 1 and 3 exist to make false. The narration's own provenance is kept
  on `DecisionRecord.narration_model_version`, and a test asserts both.

## Owed by person A (seed tuning — do not fix in the engine)

- [ ] **Beat 1 board is not calm.** 4 of 5 generated production orders read
      thin at rest; only the two anchors are healthy. Tune generated
      `daily_usage` / `usable_stock` so generated orders sit healthy. Re-check
      §16 range compliance after (23/14/21/7/14 currently).
- [ ] **Scenario 2 rests on a float tie.** COMP-104's ERP figure (420/90) and
      the Sept-6 deadline are bit-identical at 4.666…. Deterministic but
      coincidental. Purpose-seed an inventory row with an unambiguous ERP
      overstatement — spec §5 permits it, and Scenario 2 is a *specified*
      scenario, so this is implementation, not inventing a test to pass.

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
