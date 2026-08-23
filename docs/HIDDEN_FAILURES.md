# TRACE Hidden Failures & System Resilience

This document details how TRACE addresses complex, real-world hidden failures in supply chain execution, fulfilling the required submission artifact in PROJECT.md §5.

---

## 1. Overview of Hidden Failure Modes

Supply chain disruptions rarely manifest as clear, upfront errors. Instead, they often present as deceptive claims, edge-case constraints, mid-flight state shifts, or cascading multi-order dependencies. TRACE is engineered to detect, isolate, and mitigate these hidden failure modes through deterministic ratchets and provenance auditing.

---

## 2. Tested Hidden Failure Scenarios

TRACE is evaluated against nine specific judge-controlled disruption scenarios (`POST /environment/inject/{scenario_name}`):

### 1. Verification Failures & Deceptive Supplier Claims (`supplier_delay`, `contradiction`)
- **Hidden Failure:** A supplier (`SUP-21`) sends an email claiming an order has been dispatched, but carrier tracking shows `label_created_no_pickup`.
- **TRACE Resolution:** The verification module (`app/engine/verify.py`) evaluates physical tracking telemetry against supplier claims. Upon detecting the contradiction, it logs `contradicts_po_status: True` and applies an exponentially-weighted downgrade to the supplier's reliability score ($0.75 \rightarrow 0.45$). The planner automatically rejects `SUP-21` alone and selects a reliable split (`SUP-37` + `SUP-42`).

### 2. Quality & Certification Disqualifications (`quality_drop`, `uncertified`)
- **Hidden Failure:** A supplier offers low prices or fast lead times but lacks required ISO certifications or suffers a drop in quality score below component thresholds (e.g. `SUP-18` quality score $0.71 < 0.85$).
- **TRACE Resolution:** Hard pre-filtering (`app/engine/solver.py`) evaluates `set(component.required_certifications).issubset(set(supplier.certifications))` and quality thresholds *before* issuing RFQs or evaluating cost tradeoffs. Disqualified options are logged as rejected alternatives with explicit regret notes (`quality_below_threshold`).

### 3. Financial & Autonomous Threshold Overruns (`exceeds_approval`)
- **Hidden Failure:** An emergency recovery plan succeeds operationally but exceeds autonomous financial authority thresholds (e.g. recovery cost ₹123,614 vs ₹50,000 threshold).
- **TRACE Resolution:** The hard escalation ratchet (`app/engine/ratchet.py`) enforces a hard refusal stand (`verdict = escalate`, `triggers_fired = ["cost_above_threshold"]`). Autonomous ERP writes are blocked, and TRACE emits a human-in-the-loop escalation brief detailing the plan, rejected alternatives, and inaction counterfactual risks.

### 4. Concurrent Mid-Flight State Shifts (`stale_preconditions`)
- **Hidden Failure:** Environmental state changes (e.g., usable inventory consumed or a supplier quote expires) while the agent is planning.
- **TRACE Resolution:** The post-replan verification step (`app/engine/staleness.py`) captures cryptographic precondition hashes (`capture_preconditions`). If preconditions change before execution, TRACE invalidates the draft plan and re-enters execution at the earliest invalidated stage.

### 5. Infinite Re-planning & Oscillation (`reentry_loop_limit`)
- **Hidden Failure:** Continuous environment fluctuations cause an open-ended planning loop, consuming compute and delaying action.
- **TRACE Resolution:** TRACE enforces a hard bounded loop ratchet (AGENTS.md Rule 6). Re-entry is capped at a maximum of 2 passes (`MAX_REENTRY_PASSES = 2`). If the environment fluctuates past pass 2, TRACE halts re-entry and emits an escalation brief with residual staleness flags.

---

## 3. Structural Safeguards against Failure Modes

| Safeguard Layer | Mechanism | Failure Mode Prevented |
|---|---|---|
| **Deterministic Core** | Pure Python math & constraint evaluation | LLM hallucination of financial costs or inventory levels |
| **LLM-Optional Mode** | 100% regex/template fallback (`TRACE_LLM_ENABLED=false`) | API rate limiting, downtime, or network failures |
| **Single Write Guard** | `_write_erp_once` idempotent boundary | Duplicate or unapproved ERP modifications |
| **Inaction Counterfactual** | Empty supply allocation simulation | Blind execution without understanding refusal risks |
