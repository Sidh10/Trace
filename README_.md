# TRACE — Autonomous Supply Chain Disruption Control Agent

TRACE is an autonomous supply-chain disruption control agent designed to strengthen resilience in modern manufacturing environments. When supplier delays, tracking inconsistencies, or quality issues threaten production schedules, TRACE identifies risks, verifies supplier claims against physical data, evaluates Pareto-optimal sourcing alternatives, and applies budget and quality approval controls before committing changes to an ERP system.

Built by **Team devpulse** for **Hackers Occupied Pune 2026**.

---

## Key Capabilities

### 1. Provenance-Based Verification

- Evaluates supplier reliability using physical tracking signals such as `label_created_no_pickup` and carrier telemetry rather than relying solely on supplier statements.
- Automatically downgrades unsupported supplier claims. For example, SUP-21’s reliability score falls from `0.75` to `0.45` when tracking data contradicts its dispatch claim.

### 2. Multi-Supplier Split Optimization

- Uses a Pareto-optimal solver to evaluate sourcing splits across lead time, unit cost, minimum order quantity (MOQ), supplier reliability, and quality ratings.
- Satisfies production requirements for multiple orders, including `PROD-882` and `PROD-914`, while minimizing costs and preventing schedule rescheduling.

### 3. Deterministic Approval Ratchet and Escalation

- Performs arithmetic, budget validation, quality checks, and constraint evaluation entirely through deterministic Python logic.
- Uses the LLM only to parse unstructured text and narrate decisions; financial and operational comparisons are never delegated to the LLM.
- Refuses autonomous execution when the plan exceeds the configured approval threshold. For example, a plan costing ₹123,614 against a ₹50,000 limit is escalated for human approval.
- Generates a human-in-the-loop escalation brief for every rejected autonomous execution.

### 4. Inaction Counterfactual Analysis

- Evaluates the consequences of rejecting a plan and taking no action.
- Explicitly calculates unbuilt-unit shortfalls, such as `460` units for `PROD-882` and `250` units for `PROD-914`.
- Calculates the recovery-cost premium, including a documented increase of `+10.32%` over the contracted baseline.

### 5. Staleness Re-entry and Bounded Loops

- Captures snapshot hashes of all plan preconditions before execution.
- Detects concurrent changes in the operating environment and automatically re-enters the planning process.
- Limits re-entry to a maximum of two passes, ensuring that TRACE never executes a plan based on stale assumptions or enters an uncontrolled agent loop.

### 6. Fully LLM-Optional Fallback

- Runs the complete pipeline end-to-end with `TRACE_LLM_ENABLED=false`.
- Uses regex- and keyword-based text extraction together with deterministic template narration, enabling reliable operation without an LLM.

---

## Setup and Running

### Requirements

- Python 3.11 or later
- FastAPI
- Uvicorn
- Optional: `GEMINI_API_KEY` for LLM-based natural-language narration

### 1. Set Up the Environment

```bash
# Clone the repository
git clone https://github.com/Sidh10/Trace.git
cd project

# Copy the environment template
cp .env.example .env

# Optional: enable Gemini-based narration
export GEMINI_API_KEY="your-api-key-here"
export TRACE_LLM_ENABLED=true  # Set to false for the deterministic fallback
```

### 2. Start the API Server and Web UI

```bash
uvicorn app.main:app --reload --port 8080
```

Open [http://localhost:8080](http://localhost:8080) in your browser to access the interactive TRACE Coverage Board and Judge Panel simulator.

### 3. Run the Complete Test Suite

#### Deterministic fallback mode

```powershell
$env:TRACE_LLM_ENABLED="false"
python -m pytest
```

#### Gemini-enabled mode

```powershell
$env:TRACE_LLM_ENABLED="true"
python -m pytest
```

**Current benchmark:** `378 passed` — `2.42s` in deterministic mode and `3.90s` in Gemini-enabled mode.

---

## Demonstration Script and Judge Panel Scenarios

TRACE includes an embedded simulator with nine scenarios. These scenarios can be accessed through HTTP endpoints such as `POST /environment/inject/{scenario_name}` or through the built-in Judge Panel interface.

| Scenario | Injected condition | Agent response |
|---|---|---|
| `supplier_delay` | `PO-7712` is delayed. SUP-21 claims dispatch, but tracking shows no pickup. | Downgrades SUP-21’s reliability from `0.75` to `0.45` and replans the order across SUP-37 and SUP-42 for ₹123,674. |
| `exceeds_approval` | The approval threshold is lowered to ₹50,000. | Detects that the ₹123,614 plan exceeds the threshold, sets `verdict = escalate`, refuses automatic execution, and waits for a human signature. |
| `quality_drop` | SUP-18’s quality score drops to `0.71`, below the required `0.85`. | Removes SUP-18 using the `quality_below_threshold` hard filter before making RFQ calls. |
| `stale_preconditions` | A plan precondition changes during execution. | Detects staleness during post-replan verification and automatically re-enters the planning process. |
| `reentry_loop_limit` | The environment continues to mutate during replanning. | Stops at pass 2 in accordance with AGENTS.md Rule 6, preventing an uncontrolled agent loop. |

---

## Artifacts and Documentation

- **Architecture overview and workflow diagram:** [`docs/ARCHITECTURE_SUMMARY.md`](docs/ARCHITECTURE_SUMMARY.md)
- **Hidden failures and resilience documentation:** [`docs/HIDDEN_FAILURES.md`](docs/HIDDEN_FAILURES.md)
- **Real audit-trail export for PO-7712:** [`artifacts/audit_trail_po_7712.json`](artifacts/audit_trail_po_7712.json)
- **Printable escalation brief:** [`artifacts/escalation_brief_printable.html`](artifacts/escalation_brief_printable.html)
- **Raw escalation-brief JSON:** [`artifacts/escalation_brief_exceeds_approval.json`](artifacts/escalation_brief_exceeds_approval.json)

---

## License and Team

Developed by **Team TRACE** for **Hackers Occupied Pune 2026**.