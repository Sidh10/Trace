# TRACE Architecture Summary

This document provides a concise architectural overview of the TRACE Supply Chain Disruption Control Agent, as required by the submission guidelines (PROJECT.md §5).

---

## 1. System Control-Flow Diagram

The diagram below illustrates the exact end-to-end execution pipeline of TRACE, from environmental monitoring to ERP execution and audit trail recording:

```mermaid
flowchart TD
    ENV[Simulated Environment / ERP Store] -->|State & Telemetry| COVERAGE[1. Coverage Engine]
    
    subgraph Sensing & Verification
        COVERAGE -->|Days of Coverage & Shortfalls| MONITOR[2. Monitor Cycle]
        MONITOR -->|Load-bearing PO Polls & Disruption Events| VERIFY[3. Verification Engine]
        VERIFY -->|Carrier Telemetry vs Supplier Claims| PROVENANCE[Provenance-Based Reliability Update]
    end

    subgraph Optimization & Planning
        PROVENANCE -->|Updated Reliability Scores| HARDFILTER[4. Hard Pre-Filter]
        HARDFILTER -->|Cert, Quality & Budget Filtering| SOLVER[5. Pareto Solver]
        SOLVER -->|Non-dominated Combinations| PLANNER[6. Multi-Action Planner]
        PLANNER -->|Splits, Stock Allocations, Safety Stock Draw| RATCHET[7. Hard Escalation Ratchet]
    end

    subgraph Governance & Execution
        RATCHET -->|Execute Verdict| WRITE[POST /erp/update - Irreversible ERP Write]
        RATCHET -->|Escalate Verdict| BRIEF[Escalation Brief & Refusal Stand]
        BRIEF -->|Human Approval POST /agent/approval| WRITE
    end

    subgraph Audit & Resilience
        WRITE --> AUDIT[8. Provenance Graph & Audit Ledger]
        BRIEF --> AUDIT
        AUDIT --> STALENESS[Staleness Detector]
        STALENESS -->|Precondition Hash Mismatch| REENTRY[Earliest Stage Re-entry (Max 2 Passes)]
        REENTRY --> COVERAGE
    end
```

---

## 2. Core Architectural Principles

1. **Deterministic Execution Layer (AGENTS.md Rule 1)**
   - Coverage calculations, constraint checks, budget limits, quality scoring, Pareto filtering, and plan generation are 100% deterministic Python.
   - The LLM parses unstructured supplier communications (e.g. emails) into structured data and formats human-readable briefs, but never owns arithmetic or financial logic.

2. **Single Irreversible Boundary (AGENTS.md Rule 5)**
   - Exactly one action in the system is irreversible: `POST /erp/update` (`_write_erp_once`).
   - Every other stage is an idempotent read or compensable action. If an escalation trigger fires, autonomous execution is refused until a human operator signs off via `POST /agent/approval/{plan_id}`.

3. **Provenance-Based Reliability (AGENTS.md Rule 4)**
   - Supplier trust is strictly grounded in verifiable data (tracking signals vs PO commitments). Tone-based trust estimation is prohibited.
   - Contradictions dynamically downgrade reliability via exponentially-weighted smoothing: $B(t+1) = (1-\lambda)B(t) + \lambda s(t+1)$.

4. **Counterfactual Inaction Evaluation**
   - The planner computes the explicit cost of doing nothing (`cost_of_inaction`). It models unbuilt units and missed deadlines if the ratchet rejects the plan and no replacement purchase orders are placed.

5. **Bounded Re-entry & Staleness Protection (AGENTS.md Rule 6)**
   - TRACE captures precondition hashes for all plan inputs. If environmental state shifts during planning, TRACE re-enters at the earliest affected stage.
   - Re-entry is strictly bounded to a maximum of 2 passes to prevent open loop execution.
