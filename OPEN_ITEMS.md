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

## Needs a call — cost_of_inaction has no basis in this repo's docs

- [ ] `app/engine/planner.py`'s `Plan.cost_of_inaction` is `None` (with
      `cost_of_inaction_note` explaining why) because AGENTS.md,
      ARCHITECTURE.md, PROJECT.md, and BRAND.md — the only spec material in
      this repo — give no formula: no penalty clause, no shutdown-cost-per-
      day, no lost-production-value figure. ARCHITECTURE.md §7's `340000` is
      an illustrative example value in a JSON blob, not a worked
      calculation. **This is PROJECT.md §4 Beat 4's `IF REJECTED:` punchline
      and the "Leave-behind" escalation brief's headline number** — if the
      real problem statement supplies a basis (a penalty clause, a per-day
      shutdown cost), find it and wire it in before the demo; do not let
      this ship as `None` on stage. Whoever has the literal problem
      statement text should resolve this first, before item 6 (RATCHET,
      which surfaces this in its decision brief) is built.

## Needs a call — Plan shape: reversibility moved per-action

- [ ] ARCHITECTURE.md §7's `Plan` shape previously had ONE `reversibility`
      field for the whole plan. Item 5 tags reversibility on EACH action
      instead (`purchase_split` -> `compensable`, `production_reschedule`
      -> `reversible`) — see `app/engine/planner.py`'s module docstring for
      why. ARCHITECTURE.md §7 is updated to match. Item 6 (RATCHET) is the
      consuming component §7 says should agree a shape change; confirm this
      works for it, or say what needs to change.
- [ ] `safety_stock_draw` (item 5b, not built here) needs its OWN
      reversibility tag decided when it's built — `?` in ARCHITECTURE.md
      §7's example, deliberately not guessed at here.

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
