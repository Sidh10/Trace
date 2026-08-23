# TRACE — Triage, Recovery, Audit, Constrained Execution

**Hackers Occupied Pune 2026 · Supply Chain Disruption Control Agent · Team TRACE**

The why and the strategy. For rules and commands, read `AGENTS.md`. For the
technical build, read `ARCHITECTURE.md`. For naming and voice, read `BRAND.md`.

> An earlier draft used "TRACE — Twin-based Resilient Agent for Containment &
> Execution" against a freight/port-routing brief. That project is dead. Wrong
> domain. See `BRAND.md` §1 — delete on sight if you find it.

---

## 1. What we are building

An autonomous agent that keeps a simulated factory running when suppliers
fail. It monitors inventory / purchase orders / supplier email / production
schedules, detects when a disruption threatens a production deadline, verifies
supplier claims against tracking data before trusting them, builds a
multi-action recovery plan, executes it or escalates for approval, writes to a
simulated ERP, and leaves a complete decision trail.

**Target:** the problem statement's "Layer 3 — Disruption Control Agent" —
multiple simultaneous disruptions, replanning on assumption failure, order
splitting, production reprioritisation, detection of misleading supplier
claims, supplier memory, escalate-only-when-required. Layer 2 is the floor,
not the goal.

Explicitly **not**: a chatbot, an alert dashboard, a cheapest-supplier script,
or a fixed one-way workflow. The problem statement names all four as
insufficient.

---

## 2. The objective function — memorise this

| Category | Weight | What it means for us |
|---|---:|---|
| Production Continuity | **35%** | Did the line keep running? Days of coverage protected |
| Cost Control | **20%** | Solved without overspending |
| Supplier Risk Handling | **15%** | Reliability, quality, contradictions, uncertainty |
| Tool Efficiency | **10%** | Deliberate calls only; wasteful calls are penalised |
| Recovery & Replanning | **10%** | Did we adapt when new info invalidated the plan? |
| Audit & Explainability | **10%** | Traceable and justified to an operations manager |

**Zero percent for:** innovation, novelty, UI/UX, architecture elegance,
business potential, framework choice. Do not spend an hour on anything that
does not move one of the six rows above.

Continuity is weighted 1.75× Cost. "Expedite everything" loses on cost;
"cheapest supplier" loses catastrophically on continuity. **The real problem
is a regret calculation:** what the cheaper option costs you in shutdown risk.

---

## 3. The USP

Every component in this system is telegraphed by the problem statement —
claim verification, order splitting, escalation briefs, audit trails are all
named explicitly. Nothing here is conceptually unique, and it shouldn't try to
be: the rubric pays 0% for originality (§2). The differentiation is in proof,
not concept. Full framing, exact wording, and the terms to use are in
`BRAND.md` §2–3. In short:

1. **Four measured baselines** (ARCHITECTURE.md §11) — static workflow,
   cheapest-always, retry-only, and TRACE, all run on the same judge-selected
   disruption, reported with a silent-failure rate. Every other team will
   *assert* their agent worked; this is the only one that quantifies it.
   Copy-resistant structurally: baselines require a modular engine, which a
   team can't retrofit at hour 15.
2. **The judge-controlled disruption panel** (item 11) — a live robustness
   demonstration under conditions nobody rehearsed.
3. **Provenance-based skepticism, stated out loud** — reliability drops
   because tracking contradicted the supplier, never because the email
   sounded evasive.

---

## 4. Demo — five minutes, six beats

1. **(20s)** Coverage board. Every production order, two numbers each: what we
   hold, and what we hold *if suppliers keep their word*. The gap is the
   exposure. Calm — all 7 production orders read healthy at rest (on-hand +
   dependable inbound exceeds deadline).
2. **(40s)** Fire the SUP-21 delay on PO-7712. PROD-882 collapses from 15.4 days
   onto its on-hand floor of 4.3 days, against a 4.7-day deadline — the promised
   inbound evaporates and only real stock is left. Say the framing line:
   **"TRACE catches the stall before the supplier admits it"** — true, that's
   the load-bearing PO poll (item 2b), not a claim.
3. **(45s)** SUP-21 replies "dispatched." Tracking shows label created, no pickup.
   Contradiction logged, reliability drops (0.75 → 0.45) — say out loud that it dropped
   because of the tracking record, not because the email sounded evasive.
   Over-rehearse this beat; it's visceral and most teams will trust supplier
   emails at face value.
4. **(60s)** Solver output: split across SUP-42 (500 units, 4d lead time) and
   SUP-37 (450 units, 6d lead time), consuming stock while waiting. High-priority
   PROD-882 (4.7d deadline) gets SUP-42's 4-day arrival; PROD-914 (8.7d deadline)
   gets SUP-37's 6-day arrival — both orders land on time without reschedule delays.
   Show rejected options with quantified regret.
   • Under default threshold (₹150,000): plan cost (₹123,674) is within budget → verdict = **`execute`**.
   • Triggering Judge Scenario 5 ("Exceeds Approval"): threshold is lowered to ₹50,000 → verdict = **`escalate`** (`cost_above_threshold` trigger fired).
   **The agent declines to execute autonomously**, produces an escalation brief, and awaits human operator approval via `POST /agent/approval/{plan_id}` before writing to ERP. Make the refusal a beat, not a footnote. Brief ends with the **`IF REJECTED:`** inaction counterfactual showing PROD-882 (460 units unbuilt) and PROD-914 (250 units unbuilt) at risk if nothing is done.
5. **(90s)** **Hand the judge the panel. "Pick any two disruptions — at the
   same time if you want."** Staleness detector re-enters, contingency
   trigger fires, and post-replan verification catches the agent's *own* plan
   going stale and fixes it. Nobody demos their agent failing — controlled
   self-correction is the most surprising thing a judge will see that day.
6. **(25s)** Audit trail: provenance chain, tool-call count vs necessity, and
   the measured delta against the baselines (item 13). Coverage board green again.

**Optional 15-second flex, if the clock allows:** flip `TRACE_LLM_ENABLED=false`
live and show the agent still running. You have to rehearse that path anyway
(§6); no other team will have it as a demo beat.

**Leave-behind:** hand the judge the printed escalation brief — cost delta,
alternatives with regret, cost of inaction, what would have to be true for this
to be wrong. One page. Every other team leaves only the memory of a screen.

**Not on stage:** architecture diagrams, vendor/competitor comparisons,
novelty claims, academic citations. Those go in the written architecture
document (`ARCHITECTURE.md`), which judges read offline.

Beat 5 is the competition — the only thing another team cannot neutralise by
having the same architecture, and the honest test of whether we built Layer 3.
If we cannot hand over the controls, we did not.

**Demo safety:** rehearse the full run once in `TRACE_LLM_ENABLED=false`. If
the network dies or a limit hits during judging, flip the flag and keep going.

---

## 5. Submission artifacts (required)

Working demo · source code · short architecture explanation · agent workflow
diagram · sample run logs · full audit trail from one disruption · written
explanation of how the agent handles hidden failures.

A large share of the score is decided by artifacts read offline. Do not leave
these to the last twenty minutes. Voice and formatting for these: `BRAND.md`.

---

## 6. Known risks

| Risk | Mitigation |
|---|---|
| **Gemini API unverified** | Task zero, hour one (ARCHITECTURE.md §2). Plus AGENTS.md rule 2 |
| **Venue network / rate limit mid-demo** | AGENTS.md rule 2. Rehearse the LLM-off run |
| **Claude Code session limits** | Separate accounts per person; stagger driving; default to Sonnet — see CLAUDE.md |
| **Nobody owns the frontend** | One static HTML file, vanilla JS, no build step, built at hour 8 |
| **Scope** | The cut-line is real (ARCHITECTURE.md §4). Items 1–7 first, in order, end to end |
| **Concept overlap with other teams** | Expected. The rubric pays zero for originality — win on execution and beat 5 |
| **Beat 5 goes badly** | Hand over the panel only if items 8/9 are solid; otherwise demo two disruptions chosen in advance |
| **Judge undervalues the deterministic split** | One-sentence answer: frontier models score under 40% on adaptive replanning under injected triggers |
| **Integration at hour 15** | Integrate at hour 8 |

---

## 7. Standing decisions — do not relitigate

- **Trust Gate confidence product** — three invented weights, indefensible in
  Q&A. Replaced by the hard ratchet + staleness detector + reversibility tagging.
- **Separate Critic agent** — absorbed by claim verification, the ratchet, and
  post-replan verification. Extra agents cost Tool Efficiency.
- **Two-agent buyer/supplier negotiation** — high variance, maps to no rubric
  line. Describe it in the write-up as scoped and rejected. Do not build it.
- **Ripple Score / digital twin / six-agent swarm** — invented metrics and
  buzzwords; scored worst of everything evaluated.
- **MCTS / counterfactual rollouts** — Monte-Carlo-simulating our own
  deterministic simulator. Contingency triggers give the same demo beat for a
  tenth of the work.
- **Network-graph frontend** — UX is 0%.
- **Options-not-commitments** — needs PO cancellation, which the spec lacks.
- **Deliberate waiting as a strategy** — an agent that correctly waits looks
  identical to one that hung, and patience reads as recklessness against a 35%
  continuity criterion. Timing pressure comes from quote expiry instead.
- **Ant Colony Optimization / "bio-inspired swarm" (RIPPLE, HIVE, MYCELIUM)** —
  scoped and rejected for three reasons, in order of decisiveness. (a) The plan
  space is **exactly enumerable**: ≤2 of ~14 suppliers is ~120 combinations ×
  corner-point quantities × a few stock/reschedule actions. Brute-force
  enumeration finds the *provably optimal* plan in milliseconds; ACO runs 2,000
  stochastic samples to find an *approximate* one. Strictly dominated — never
  better, sometimes worse. (b) It's nondeterministic, so the same disruption can
  yield a different plan on stage than in rehearsal, and "why this plan?" becomes
  "the swarm converged there" — fatal for a 10% audit criterion. (c) Fifteen
  invented weights (RippleScore's five, PlanScore's six, plus α, β, ρ, Q) against
  the three that got the Trust Gate cut.
  **The Q&A answer if a judge raises it:** the only part of ACO that fits this
  problem, we already have. Pheromone evaporation `τ ← (1−ρ)τ + Δτ` and our
  reliability update `B(t+1) = (1−λ)B(t) + λs(t+1)` are the same recurrence —
  ours just isn't wearing a costume.

- **Project Nexus (GNN + RL freight reroute agent)** — a real, working prior
  project, but wrong domain: no suppliers, no production line, no coverage
  math, and a human clicks "apply" rather than the system deciding. Scored
  1.15/10 against this rubric. Do not re-skin it — the missing pieces
  (supplier verification, coverage, approval boundaries, audit provenance)
  aren't a rename away. Reusable only as background knowledge: the synthetic
  data generator pattern and the FastAPI orchestrator shape, not any code.

Nothing here is novel research. It is a correct, disciplined implementation of
a well-specified brief. That is what the rubric pays for.
