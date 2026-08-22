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
