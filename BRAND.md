# BRAND.md

Naming, terminology, and voice. Read this before writing anything a judge,
teammate, or the README will show a human — escalation-brief copy, demo
narration, slide text, code comments that double as documentation.

This exists because the team has, in earlier planning, generated at least
three incompatible names for this project and inconsistently referred to the
same component two different ways. This file is the tie-breaker.

---

## 1. Name

**TRACE — Triage, Recovery, Audit, Constrained Execution.**

**Banned:** "TRACE — Twin-based Resilient Agent for Containment & Execution."
This was an earlier, wrong-domain (freight/port-routing) project and shares
only the acronym. If this phrase appears anywhere — slides, old docs, a
teammate's memory — it refers to a dead project. Delete on sight.

---

## 2. One-line pitch

> A procurement disruption agent that never lets a supplier's word be the last
> word — a deterministic solver picks the recovery plan, every claim gets
> checked against tracking data before it's trusted, and escalation is a hard
> rule, not a hunch.

---

## 3. The USP — say this, not a feature list

The rubric pays zero for originality, so don't pitch novelty. Pitch proof.

> "Most agents in this room will tell you they kept the line running. We'll
> let you pick the disruption, and then show you exactly how many days of
> production we saved and what it cost — against an agent that didn't bother
> verifying anything."

Three layers, in order of what survives a competitor with the identical
architecture:

1. **The measurement** — four measured baselines plus a silent-failure rate
   (build order item 13, spec in ARCHITECTURE.md §11). Every other team
   asserts their agent worked; this is the only one that quantifies it.
2. **The handover** — the judge-controlled disruption panel (item 11). A
   demonstration under conditions nobody rehearsed, not a claim.
3. **The stated skepticism** — "reliability dropped because tracking
   contradicted the supplier, never because the email sounded evasive." One
   sentence, costs nothing, signals the team thought about how trust gets
   gamed.

---

## 4. Terminology — use this term, not that one

| Use | Not | Why |
|---|---|---|
| days of coverage | buffer, runway, stock-days | The exact rubric-linked metric name (35% category). Keep it identical everywhere |
| escalation brief | alert, notification, flag | The spec (§4.9) explicitly wants a decision-ready brief, not an alert |
| provenance graph | audit log, decision log | It's a typed graph (Support/Contradict/Invalidate/...), not a flat log — the name should say so |
| naive agent | null agent, dumb agent, baseline bot | Pick one term. "Naive" is accurate (cheapest-supplier-always, no verification) and reads clearly on a slide |
| judge-controlled disruption panel | chaos injector, hidden-test harness | "Chaos injector" undersells it — the judge drives it, we don't |
| regret (on a rejected alternative) | opportunity cost, trade-off | Matches the spec's own "alternatives considered" language and the decision-brief framing |
| reliability score | trust score, confidence score | "Confidence" is reserved — see banned terms below, it's tied to the rejected Trust Gate |

**Banned outright** — these belong to rejected ideas or the wrong-domain
lineage. Using them signals the team hasn't internalized why they were cut:

- "digital twin," "ripple score," "blast radius," "control tower," "self-healing network" — freight-lineage terms, wrong domain, invented metrics
- "confidence gate," "trust gate" — the rejected confidence-product design (AGENTS.md, "Do not build")
- "ant colony," "pheromone," "swarm," "bio-inspired," "nature-inspired," "hive," "mycelium," "ecosystem intelligence" — the rejected ACO proposal (PROJECT.md §7). Note: we do use `(1−λ)B(t) + λs(t+1)` for reliability, which is the same recurrence as pheromone evaporation — call it an **exponentially weighted reliability update**, never a pheromone
- any freight vocabulary: port, carrier, corridor, shipment, route, hazmat

---

## 5. Voice

Plain, confident, quantified. State what the agent did, in numbers, not what
it's capable of in the abstract.

**Avoid:** revolutionary, cutting-edge, next-generation, seamless, powerful,
state-of-the-art, game-changing, AI-powered (as a selling point — of course
it's AI-powered, that's the assignment). If a sentence works with any of these
words deleted, delete them.

**Prefer:** "the agent split the order across two suppliers and kept the line
running" over "our innovative agent leverages advanced AI to optimize supply
chain resilience."

If a judge could read a sentence and not learn what the system actually did,
rewrite it.

---

## 6. README / visual style

No badge wall. No emoji section headers. No architecture-diagram-as-hero-image.
UX is worth 0% of the rubric (PROJECT.md §2) — decorative README energy signals
the team optimized for looking finished over being correct, which is the exact
failure mode of the wrong-domain "Project Nexus" precedent.

The README gets written **after** the cut-line components work and real run
logs exist — not before. Don't draft placeholder results; a number that isn't
from an actual run doesn't go in the README, full stop.
