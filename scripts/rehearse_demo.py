import json
from datetime import datetime, timezone
from app.environment import seed_data
from app.environment.seed_data import build_store
from app.engine.coverage import compute_coverage
from app.engine.monitor import run_monitor_cycle
from app.engine.verify import run_verification_cycle
from app.api.routes import run_pipeline, resolve_approval, reset_orchestrator_state
from app.environment.routes import inject_scenario
from app import config

def run_demo_rehearsal():
    now = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
    config.TRACE_APPROVAL_THRESHOLD = 150000.0

    print("==========================================================================")
    print("               TRACE FULL DEMO SCRIPT REHEARSAL (LIVE RUN)               ")
    print("==========================================================================")

    # ----------------------------------------------------------------------
    # BEAT 1: At Rest Board
    # ----------------------------------------------------------------------
    print("\n--- BEAT 1: AT REST BOARD ---")
    store = build_store()
    seed_data.STATE = store
    cov1 = compute_coverage(store, now=now)
    healthy_count = sum(1 for r in cov1.results if r.status == "healthy")
    critical_count = sum(1 for r in cov1.results if r.status == "critical")
    at_risk_count = sum(1 for r in cov1.results if r.status == "at_risk")

    print(f"Total Production Orders: {len(cov1.results)}")
    print(f"Status Breakdown: {healthy_count} Healthy, {at_risk_count} At-Risk, {critical_count} Critical")
    for r in cov1.results:
        print(f"  * {r.production_order_id} ({r.component_id}): stock={r.usable_stock}, on-hand={r.days_of_coverage_on_hand:.1f}d, total_coverage={r.days_of_coverage:.1f}d, deadline={r.days_to_deadline:.1f}d -> {r.status.upper()}")

    # ----------------------------------------------------------------------
    # BEAT 2: Disruption Injection (PO-7712 Delayed) & Claim Elicitation
    # ----------------------------------------------------------------------
    print("\n--- BEAT 2: DISRUPTION INJECTION (PO-7712 DELAYED) ---")
    store.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Status check on PO-7712",
        body="Any update on PO-7712?",
    )
    cov_pre = compute_coverage(store, now=now)
    mon_pre = run_monitor_cycle(store, coverage=cov_pre, now=now)
    ver_pre = run_verification_cycle(store, coverage=cov_pre, monitor=mon_pre, now=now)

    store.purchase_orders["PO-7712"].status = "delayed"
    cov2 = compute_coverage(store, now=now)
    p882 = next(r for r in cov2.results if r.production_order_id == "PROD-882")
    print(f"PO-7712 Status: delayed")
    print(f"PROD-882 (COMP-104): on-hand floor={p882.days_of_coverage_on_hand:.1f}d vs deadline={p882.days_to_deadline:.1f}d -> STATUS TURNED {p882.status.upper()}")

    # ----------------------------------------------------------------------
    # BEAT 3: Claim Verification Cycle
    # ----------------------------------------------------------------------
    print("\n--- BEAT 3: CLAIM VERIFICATION CYCLE ---")
    sup21 = store.suppliers["SUP-21"]
    print(f"Contradictions Found: {len(ver_pre.contradictions())}")
    for c in ver_pre.contradictions():
        print(f"  * Contradiction: {c.contradiction_reason}")
    print(f"SUP-21 Reliability Downgrade: 0.75 -> {sup21.reliability_score:.2f}")

    # ----------------------------------------------------------------------
    # BEAT 4: Solver, Planner & Hard Ratchet (Execute vs Escalate)
    # ----------------------------------------------------------------------
    print("\n--- BEAT 4A: SOLVER & RATCHET (PO-7712 DEFAULT THRESHOLD 150,000) ---")
    run_default = run_pipeline(store, component_id="COMP-104", now=now)
    brief_def = run_default.brief
    print(f"Decision: {run_default.decision.upper()}")
    print(f"Plan ID: {brief_def.plan_id}")
    print(f"Chosen Combination: {[m.supplier_id for m in brief_def.chosen_plan.chosen_combination.members]} split")
    print(f"Total Cost: RS {brief_def.total_cost:,.2f} (vs threshold RS {brief_def.approval_threshold:,.2f})")
    print(f"Reschedule Actions Count: {len(brief_def.chosen_plan.reschedule_actions())}")
    print(f"ERP Writes Made: {len(run_default.erp_writes)} (1 store_plan decision record + {len(brief_def.chosen_plan.purchase_actions())} create_alternate_po, one per supplier in the split)")
    for w in run_default.erp_writes:
        action_type = w.action.get('type') if isinstance(w.action, dict) else getattr(w.action, 'type', 'N/A')
        print(f"  * ERP Update ({w.update_id}): Action={action_type}")

    # BEAT 4B runs on its own fresh store/environment (Scenario 5's low
    # approval threshold is a separate judge-triggered disruption, not a
    # continuation of Beat 4A). `inject_scenario()` always mutates
    # `seed_data.STATE`, so STATE must point at store5 before the injection
    # for it to land on the store this beat actually pipes through; and
    # `reset_orchestrator_state()` must run first, or `run_pipeline` treats
    # store5 as a continuation of Beat 4A's cached "COMP-104" run (same
    # component_id, different store) and replays/re-enters against the
    # wrong prior instead of solving store5 fresh.
    print("\n--- BEAT 4B: JUDGE SCENARIO 5 (EXCEEDS APPROVAL: THRESHOLD 50,000) ---")
    store5 = build_store()
    seed_data.STATE = store5
    reset_orchestrator_state()
    store5.send_supplier_message(
        supplier_id="SUP-21",
        to="supplier21@example.com",
        subject="Status check on PO-7712",
        body="Any update on PO-7712?",
    )
    inject_scenario("exceeds_approval")
    run_esc = run_pipeline(store5, component_id="COMP-104", now=now)
    brief_esc = run_esc.brief
    print(f"Decision: {run_esc.decision.upper()}")
    print(f"Triggers Fired: {brief_esc.triggers_fired}")
    print(f"Total Cost: RS {brief_esc.total_cost:,.2f} (vs threshold RS {brief_esc.approval_threshold:,.2f})")
    print(f"Awaiting Approval: {run_esc.awaiting_approval}")
    print(f"ERP Writes Made: {len(run_esc.erp_writes)} (Refused autonomous write)")
    print(f"Inaction Counterfactual (If Refusal Stands):")
    for r in brief_esc.cost_of_inaction.production_orders_at_risk:
        print(f"  * {r.production_order_id} ({r.priority} priority): {r.units_unbuilt} units short -- {r.inaction_impact}")

    # ----------------------------------------------------------------------
    # BEAT 5: Human Approval & Irreversible ERP Write
    # ----------------------------------------------------------------------
    print("\n--- BEAT 5: HUMAN OPERATOR APPROVAL & ERP WRITE ---")
    run_approved = resolve_approval(store5, plan_id=brief_esc.plan_id, approved=True, approved_by="Human Operator")
    print(f"Approval Outcome: {run_approved.approval_outcome.upper()}")
    print(f"Awaiting Approval: {run_approved.awaiting_approval}")
    print(f"ERP Writes Executed: {len(run_approved.erp_writes)}")
    for w in run_approved.erp_writes:
        print(f"  * ERP Update Executed ({w.update_id}): Action={w.action.get('type') if isinstance(w.action, dict) else getattr(w.action, 'type', 'N/A')}")

    # ----------------------------------------------------------------------
    # BEAT 6A: Staleness Detection & Bounded Loop Re-entry
    # ----------------------------------------------------------------------
    # A genuine two-pass demonstration of item 8, on its own fresh store6 —
    # same isolation reasoning as Beat 4B. Pass 1 plans against what the ERP
    # believes at that moment (PO-7712 delayed, stock not yet corrected).
    # Only THEN do we apply the warehouse correction (`stale_erp`: ERP
    # overstates current_stock relative to what the warehouse actually has
    # usable) and re-run — that is what gives the staleness detector
    # something real to catch, and it is store6's own prior plan that gets
    # superseded, not a plan carried over from a different beat's store.
    print("\n--- BEAT 6A: STALENESS DETECTION & BOUNDED RE-ENTRY ---")
    # Beat 4B's `exceeds_approval` injection left the threshold at 50,000 for
    # the rest of the process (same leftover-global issue as Beat 6B below).
    # Beat 6A is demonstrating staleness/re-entry, not the low-threshold
    # scenario, so it needs the default threshold restored or its own plan
    # escalates on the wrong basis.
    config.TRACE_APPROVAL_THRESHOLD = 150000.0
    store6 = build_store()
    seed_data.STATE = store6
    reset_orchestrator_state()
    store6.purchase_orders["PO-7712"].status = "delayed"
    run_initial = run_pipeline(store6, component_id="COMP-104", now=now)
    print(f"Pass 1 (pre-correction) Plan ID: {run_initial.brief.plan_id}, Decision: {run_initial.decision.upper()}")

    inject_scenario("stale_erp")
    run_stale = run_pipeline(store6, component_id="COMP-104", now=now)
    print(
        f"Explanation: `{run_initial.brief.plan_id}` was the plan store6 executed on pass 1, "
        "before the warehouse-vs-ERP stock mismatch was known. Once the correction "
        f"landed, the orchestrator detected the contradiction and re-entered planning, "
        f"generating `{run_stale.brief.plan_id}` to supersede it."
    )
    print(f"Post-Replan Verified: {run_stale.post_replan_verified}")
    print(f"Reentered at Stage: {run_stale.reentered_at_stage}")
    print(f"Stages Executed Trail: {run_stale.stages}")

    # ----------------------------------------------------------------------
    # BEAT 6B: Closing Audit Trail, Tool-Call Summary & Multi-Baseline Proof
    # ----------------------------------------------------------------------
    print("\n--- BEAT 6B: CLOSING PROOFS & AUDIT SUMMARY ---")
    print("1. PROVENANCE GRAPH FOR PO-7712 RUN:")
    print(f"   Total Edges: {len(run_default.graph.edges)} (Saved to artifacts/audit_trail_po_7712.json)")
    for edge in run_default.graph.edges[:5]:
        print(f"   * [{edge.relation}] {edge.from_node} -> {edge.to_node} ({edge.produced_by_module})")

    print("\n2. TOOL-CALL AUDIT (MADE VS AVOIDED):")
    ta = run_default.graph.tool_audit
    print(f"   Calls Made: {ta.total_calls_made}, Calls Avoided: {ta.total_calls_avoided}")
    print(f"   Verdict: {ta.necessity_verdict}")

    print("\n3. MULTI-BASELINE HARNESS COMPARISON (ITEM 13):")
    # Beat 4B's `exceeds_approval` injection left config.TRACE_APPROVAL_THRESHOLD
    # at 50,000 for the rest of the process. Left uncorrected, every variant
    # below escalates on that leftover threshold instead of its own plan cost,
    # which erases the differentiation this harness exists to show (e.g.
    # cheapest_always finding a cheaper, lower-quality plan than trace) and
    # makes all five variants print identical "escalate / RS 0.00" rows.
    config.TRACE_APPROVAL_THRESHOLD = 150000.0
    from app.engine.baseline import run_baseline_comparison
    base = run_baseline_comparison("supplier_delay", now=now)
    for v_name, summary in base.variants.items():
        print(f"   * Variant: {v_name:<25} | Spend: RS {summary.total_spend:,.2f} | Decision: {summary.decision} | Silent Failure: {summary.silent_failure}")

    print("\n==========================================================================")
    print("             DEMO SCRIPT REHEARSAL COMPLETED SUCCESSFULLY                 ")
    print("==========================================================================")

if __name__ == "__main__":
    run_demo_rehearsal()
