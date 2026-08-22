# OPEN_ITEMS.md

Live tracker. Delete lines as they close. Not a design doc — see ARCHITECTURE.md.

## Owed by whoever builds item 8

- [ ] **A possible staleness trigger, not yet wired.** When VERIFY
      (`app/engine/verify.py`) confirms a specific PO's claim is contradicted
      (`ClaimVerification.contradicted=True`), should COVERAGE stop crediting
      that PO's quantity — i.e. re-run `compute_coverage` with it excluded,
      not just downgrade the supplier's reliability score? This is a
      per-shipment fact, not a reliability-average threshold, so it does not
      run into the rule-7 problem the reliability-coupling question did (see
      `app/engine/verify.py`'s module docstring). Item 8's "re-enter at the
      earliest invalidated stage" is the natural place for this, not a change
      to `coverage.py`'s `dependable_inbound()` filter itself.

## REQUIRED for item 6 — nothing currently catches a plan that misses its deadline

- [ ] **Confirmed gap, not a hypothesis.**
      `tests/test_planner.py::test_ADVERSARIAL_reliability_first_choice_can_silently_blow_a_deadline_reschedule_would_have_avoided`
      constructs a real Pareto set — SUP-RELIABLE (0.95 reliability, 20-day
      lead) vs SUP-RISKY (0.5 reliability, 2-day lead) — against a
      HIGH-priority order with a 5-day deadline. `SELECTION_RULE` (reliability
      first, as designed and already flagged above) picks SUP-RELIABLE.
      `allocate_stock` / `_reschedule_actions` compute the consequence
      correctly and honestly: `delay_days=15` — the order lands 3x past its
      original 5-day deadline. **`run_planner` returns this as a normal,
      well-formed `Plan`, indistinguishable in shape from any other plan.**
      Nothing anywhere — not `Plan`, not `planner.py`, not any other built
      component — flags that this specific reschedule might be unacceptable,
      or that a REJECTED alternative (SUP-RISKY) would have hit the deadline
      cleanly. `RejectedAlternative.regret` for SUP-RISKY says only "lost on
      reliability_score" — it never mentions that picking it would have made
      the deadline the chosen option missed.
- [ ] **This is AGENTS.md rule 3, already named, not yet implemented
      anywhere:** *"Cost above threshold, no supplier meets deadline, or
      quality risk → escalate."* "No supplier meets deadline" is one of
      exactly three hard, spec-mandated escalation triggers — item 6
      (RATCHET) is where this belongs (item 5/PLANNER's job is to build the
      best plan it can from what SOLVER hands it, not to judge whether that
      plan is good enough to execute autonomously — that judgment is
      RATCHET's entire reason to exist). **Explicit requirement for item 6:**
      before executing (or instead of silently executing) ANY plan
      containing a `production_reschedule` action, check whether
      `delay_days` represents a deadline miss serious enough to escalate
      rather than execute silently — at minimum, whenever the CHOSEN
      combination missed a deadline that a REJECTED, otherwise-valid
      alternative would have met. This is not optional polish; without it,
      a plan can silently reschedule a high-priority order by weeks and ship
      it as if it were routine.
- [ ] Not fixed in `planner.py` deliberately — item 5's job is to build the
      best plan from what SOLVER hands it and report the real consequence
      (which it now does, correctly); judging whether that consequence is
      acceptable to execute without a human is item 6's job specifically.
      Fixing it here would mean PLANNER quietly deciding execute-vs-escalate,
      which ARCHITECTURE.md §3's own diagram and this session's item 5
      instructions both reserve for RATCHET alone.

## BLOCKING for the demo — cost_of_inaction, searched twice, genuinely absent

- [ ] `app/engine/planner.py`'s `Plan.cost_of_inaction` is `None`. Searched
      TWICE (re-checked specifically for SLA terms, contract clauses,
      damages, late fees — anything consequence-framed rather than
      process-framed) — AGENTS.md, ARCHITECTURE.md, PROJECT.md, BRAND.md
      give no formula. ARCHITECTURE.md §5 explicitly forbids "customer SLA
      tiers" as out-of-domain, which is further evidence, not just absence.
      Also tried the ONE concrete fallback proxy floated during this build
      ("units short × their committed sale price") and confirmed it has NO
      data to compute from: every monetary field in `schemas.py`
      (`PurchaseOrder`/`SupplierRecord`/`RFQQuote.unit_price`,
      `ApprovalCheckRequest.estimated_cost`) is a PROCUREMENT cost;
      `ProductionOrder` carries no price field at all. Two further fallbacks
      built only from data that DOES exist were considered and rejected:
      pricing the shortfall at a real quote is circular with `total_cost`
      (same purchase, relabeled, not independent information); pricing
      downtime by day requires inventing a time horizon for "how long does
      inaction last," which is the same rule-7 violation one step removed.
      Full reasoning in `app/engine/planner.py`'s module docstring and
      `COST_OF_INACTION_NOTE` (the actual runtime-visible note, not just a
      code comment).
      **This is PROJECT.md §4 Beat 4's `IF REJECTED:` punchline and the
      "Leave-behind" escalation brief's headline number — it cannot ship as
      `None` on stage.** Whoever has the literal problem statement text
      needs to find a real basis (penalty clause, per-day shutdown cost,
      lost-revenue figure) and wire it into `planner.py` before the demo, or
      the team needs to decide how Beat 4 and the leave-behind brief handle
      an admittedly-unknown cost of inaction on stage. Resolve before item 6
      (RATCHET) surfaces this in its decision brief.

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

## Needs a call — selection rule puts reliability ahead of lead time

- [ ] `planner.py`'s `SELECTION_RULE` ranks reliability_score above
      lead_time_days (see the module docstring's full reasoning: coverage's
      own "gap is the exposure to supplier claims" framing, and BRAND.md's
      pitch that a claim gets checked "before it's trusted" — trusted enough
      to change what gets picked, not just logged). Concretely, against the
      real current dataset (item 4's post-verify Pareto set for COMP-104),
      this picks the SUP-37+SUP-42 split over SUP-21 alone, even though
      SUP-21 is 3 days faster and ~₹13,500 cheaper — because item 3 already
      downgraded SUP-21 to 0.45 reliability after the PO-7712 contradiction.
      **This is a genuine design call, not a spec-given rule.** A real risk
      of ANY strict lexicographic tier: a trivial reliability difference
      (0.81 vs 0.80) would outrank a huge lead-time gap (6 days vs 60) just
      as decisively as the real 0.81-vs-0.45 gap did. A more nuanced version
      — gating on the DISCRETE fact "was this specific claim contradicted"
      (from `ClaimVerification.contradicted`) rather than the continuous
      `reliability_score` — would avoid that, but needs `planner.py` to
      consume a `VerificationReport` too, a real interface change not made
      here. Flagging for whoever revisits this, not fixed unilaterally.
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

## Needs a call — "certified" interpretation

- [ ] `app/engine/solver.py`'s hard filter reads "certified" as "holds at
      least one certification" (any cert, not specifically ISO-9001).
      Grounded in `seed_data.py`'s own SUP-18 comment ("fails the
      certified-supplier requirement, §6") and ARCHITECTURE.md §7's worked
      example ("disqualified — uncertified") — both describe the failure as
      lacking certification entirely, not lacking one specific credential.
      This session doesn't have the literal problem-statement text to check
      further. If §6 actually names a specific required certification
      (ISO-9001, say), `_is_certified` in `solver.py` is a one-line fix, not
      a design change — flagging so whoever DOES have that text can confirm
      or correct it before the demo.

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

## Owed by whoever builds item 7

- [ ] **`ParsedClaim.model_version` is ready for `ProvenanceEdge.model_version`
      — read it, don't recompute it.** `verify.py`'s `parse_claim` now sets
      `model_version` to `app.llm.gemini_client.MODEL_VERSION` (the real
      pinned string, e.g. `"gemini-3.6-flash"`) on a genuine LLM success, and
      the literal `"deterministic"` on every other path — LLM disabled, or
      the LLM call raised and fell through. That's exactly ARCHITECTURE.md
      §7's `"gemini-<version> | deterministic"` format. When building
      `ProvenanceEdge`, populate `model_version` from
      `ClaimVerification.claim.model_version`; don't re-derive it from
      `parsed_by` or hardcode the model string a second time.

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
