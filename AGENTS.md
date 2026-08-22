# AGENTS.md

TRACE — Supply Chain Disruption Control Agent. Hackers Occupied Pune 2026, Team TRACE.

Read this before writing or generating any code in this repo. This file is the
canonical, enforced rule set — it overrides a plausible-sounding default every
time. On a rules conflict, this file wins. On a strategy/narrative conflict,
`PROJECT.md` wins.

Companion files — read the relevant one before starting work, don't guess:

| File | What it's for |
|---|---|
| `PROJECT.md` | Why we're building this — rubric, USP, demo script, risks |
| `ARCHITECTURE.md` | How it's built — stack, control flow, build order, cut-line, data model, repo layout |
| `BRAND.md` | Naming, terminology, voice — read before writing any copy (README, escalation-brief text, demo narration, slides) |

---

## Setup & commands

```bash
# environment
cp .env.example .env            # fill in GEMINI_API_KEY
export TRACE_LLM_ENABLED=true   # set false to run the deterministic-only path

# run
uvicorn app.main:app --reload --port 8080

# test
pytest

# task zero — run before anything else exists. See ARCHITECTURE.md §2.
python scripts/task_zero_gemini_check.py
```

If `pytest` or `app/main.py` don't exist yet, that means the component
hasn't been built — check ARCHITECTURE.md §4 before assuming something's broken.

---

## Non-negotiable rules — canonical, do not soften these

1. **The LLM never owns arithmetic, constraints, or comparisons.** It does two
   things only: (a) parse unstructured supplier email / RFQ text into
   structured fields, (b) turn a finished deterministic decision into plain
   language. Coverage math, certification checks, budget checks, claim
   comparison, and plan selection are deterministic Python — full stop.
2. **LLM-optional mode is mandatory.** The whole pipeline must run end to end
   with `TRACE_LLM_ENABLED=false` — regex/keyword parsing for supplier
   messages, template strings for narration. Build the deterministic path
   first; the LLM is bolted on top, never a dependency in the critical path.
3. **Escalation triggers are a hard ratchet.** Cost above threshold, no
   supplier meets deadline, or quality risk → escalate. No confidence score
   overrides this and the LLM cannot argue past it. Executing above the
   approval threshold is a bug, not a judgement call.
4. **Trust is provenance-based, never tone-based.** A supplier's reliability
   drops because tracking data contradicted them — never because their email
   "sounded evasive." If you're inferring trust from phrasing, stop.
5. **Exactly one action is irreversible: `POST /erp/update`.** Everything else
   is an idempotent read or a compensable message. Tag actions accordingly.
6. **Bounded loop — not a straight line, not an open agent loop.** See
   `ARCHITECTURE.md` §3. A one-way pipeline can't replan; an open loop is a
   demo liability.
7. **Never invent a metric and display it as a finding.** No made-up risk
   scores, no confidence products with invented weights. Every number on
   screen must be derivable from the data — and don't dress up a plain
   heuristic with a technical-sounding name it hasn't earned (e.g. don't call
   exponential smoothing "Bayesian").
8. **Don't build a sandbox feature the agent needs in order to look clever.**
   We own the simulator, so anything we add invites "you wrote the test you
   pass." Only build environment features the problem statement specifies —
   see `ARCHITECTURE.md` §5.

---

## Do not build — already evaluated and rejected

MILP/OR-Tools, LangGraph, PostgreSQL, a knowledge graph, React/npm/any build
step, a separate "Critic" agent, two-agent buyer/supplier negotiation, a Trust
Gate confidence product, MCTS/counterfactual rollouts, a network-graph
frontend, PO cancellation logic, deliberate-waiting-as-a-strategy.

Full reasoning for each is in `PROJECT.md` under Standing Decisions. Don't
re-propose these and don't re-derive the reasoning — it's already been done,
and re-litigating costs build hours you don't have.

---

## Multi-tool note

Antigravity and other AGENTS.md-aware tools read this file natively. Claude
Code does not read AGENTS.md on its own — `CLAUDE.md` in this repo imports it
on the first line, so Claude Code sessions get identical content. **Edit rules
here, not in CLAUDE.md** — CLAUDE.md should only ever hold Claude-Code-specific
additions.
