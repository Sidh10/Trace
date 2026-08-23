# TRACE — Autonomous Supply Chain Disruption Control Agent

TRACE is an autonomous supply chain disruption control agent built for modern manufacturing resilience. When supplier delays, tracking contradictions, or quality issues threaten production schedules, TRACE identifies risks, verifies claims against physical data, explores Pareto-optimal sourcing alternatives, and enforces budget & quality approval ratchets before committing changes to ERP.

Built for **Hackers Occupied Pune 2026** by Team TRACE.

---

## Key Capabilities

1. **Provenance-Based Verification**
   - Reliability is evaluated against physical tracking signals (`label_created_no_pickup`, carrier telemetry) rather than supplier tone.
   - Automatically downgrades unsupportable supplier claims (e.g. SUP-21 score drops 0.75 → 0.45 upon tracking contradiction).

2. **Multi-Supplier Split Optimization**
   - Pareto-optimal solver evaluates candidate splits across lead times, unit costs, minimum order quantities (MOQ), supplier reliability, and quality ratings.
   - Automatically satisfies multi-order production demand (`PROD-882`, `PROD-914`) while minimizing costs and preventing schedule reschedules.

3. **Deterministic Hard Ratchet & Escalation**
   - Arithmetic, budget checks, quality thresholds, and constraint evaluation are executed entirely in deterministic Python. The LLM parses unstructured text and narrates decisions, but never owns financial or operational comparisons.
   - Refuses autonomous execution when plan costs exceed approval thresholds (e.g., ₹123,614 plan cost vs ₹50,000 limit) and generates human-in-the-loop escalation briefs.

4. **Inaction Counterfactual Analysis**
   - Every plan evaluates what happens if the ratchet rejects the plan and does nothing: explicitly computing unbuilt unit shortfalls (`PROD-882`: 460 units, `PROD-914`: 250 units) and recovery cost premiums (+10.32% over contracted baseline).

5. **Staleness Re-entry & Bounded Loops**
   - Captures snapshot hashes of plan preconditions before execution.
   - Detects concurrent environment changes and re-enters planning (bounded to max 2 passes) to guarantee zero execution on stale assumptions.

6. **100% LLM-Optional Fallback**
   - Complete pipeline runs end-to-end with `TRACE_LLM_ENABLED=false` using regex/keyword text extraction and deterministic template narration.

---

## Setup & Running

### Requirements
- Python 3.11+
- Fast-API & Uvicorn
- Optional: `GEMINI_API_KEY` for LLM natural-language narration

### 1. Environment Setup
```bash
# Clone repository
git clone https://github.com/Sidh10/Trace.git
cd project

# Copy environment template
cp .env.example .env

# Optional: set Gemini API key if LLM narration is enabled
export GEMINI_API_KEY="your-api-key-here"
export TRACE_LLM_ENABLED=true  # set false for 100% deterministic fallback
```

### 2. Run API Server & Web UI
```bash
uvicorn app.main:app --reload --port 8080
```
Open [http://localhost:8080](http://localhost:8080) in your browser to view the interactive TRACE Coverage Board and Judge Panel simulator.

### 3. Run Full Test Suite
```bash
# Deterministic fallback mode
$env:TRACE_LLM_ENABLED="false"
python -m pytest

# Gemini-enabled mode
$env:TRACE_LLM_ENABLED="true"
python -m pytest
```
*Current benchmark: 373 passed (1.46s deterministic mode / 1.43s Gemini mode).*

---

## Demonstration Script & Judge Panel Scenarios

TRACE features an embedded 9-scenario simulator accessible via HTTP endpoints (`POST /environment/inject/{scenario_name}`) or the built-in UI Judge Panel:

| Scenario | Injected Condition | Agent Response |
|---|---|---|
| `supplier_delay` | `PO-7712` delayed; SUP-21 claims dispatch while tracking shows no pickup | Downgrades SUP-21 reliability (0.75 → 0.45); replans split (SUP-37 + SUP-42) for ₹123,674 |
| `exceeds_approval` | Approval threshold lowered to ₹50,000 | Plan cost ₹123,614 > ₹50,000 threshold → **`verdict = escalate`**; Refuses auto-execution; awaits human signature |
| `quality_drop` | SUP-18 quality score drops to 0.71 < 0.85 required | Hard filter drops SUP-18 (`quality_below_threshold`) before spending RFQ calls |
| `stale_preconditions` | Precondition changed mid-flight | Post-replan verification detects staleness; re-enters planning automatically |
| `reentry_loop_limit` | Continuous environment mutations | Bounded ratchet stops at pass 2 (AGENTS.md Rule 6), avoiding open agent loops |

---

## Artifacts & Documentation

- **Architecture Overview & Workflow Diagram:** [`docs/ARCHITECTURE_SUMMARY.md`](docs/ARCHITECTURE_SUMMARY.md)
- **Hidden Failures & Resilience Documentation:** [`docs/HIDDEN_FAILURES.md`](docs/HIDDEN_FAILURES.md)
- **Real Audit Trail Export (PO-7712):** [`artifacts/audit_trail_po_7712.json`](artifacts/audit_trail_po_7712.json)
- **Printable Escalation Brief:** [`artifacts/escalation_brief_printable.html`](artifacts/escalation_brief_printable.html)
- **Escalation Brief Raw JSON:** [`artifacts/escalation_brief_exceeds_approval.json`](artifacts/escalation_brief_exceeds_approval.json)

---

## License & Team

Developed by **Team TRACE** for **Hackers Occupied Pune 2026**.
