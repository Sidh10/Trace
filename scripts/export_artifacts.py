import os
import json
from datetime import datetime, timezone
from app.environment.seed_data import build_store
from app.environment.routes import inject_scenario
from app.api.routes import run_pipeline

def export_all():
    os.makedirs('artifacts', exist_ok=True)
    os.makedirs('docs', exist_ok=True)

    now = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)

    # 1. Export PO-7712 audit trail JSON
    store1 = build_store()
    store1.send_supplier_message(supplier_id='SUP-21', to='supplier21@example.com', subject='x', body='Any update on PO-7712?')
    store1.purchase_orders['PO-7712'].status = 'delayed'
    run1 = run_pipeline(store1, component_id='COMP-104', now=now)
    with open('artifacts/audit_trail_po_7712.json', 'w', encoding='utf-8') as f:
        json.dump(run1.graph.model_dump(mode='json'), f, indent=2)
    print('Exported artifacts/audit_trail_po_7712.json')

    # 2. Export exceeds_approval escalation brief JSON & printable HTML
    store2 = build_store()
    inject_scenario('exceeds_approval')
    run2 = run_pipeline(store2, component_id='COMP-104', now=now)
    brief = run2.brief

    with open('artifacts/escalation_brief_exceeds_approval.json', 'w', encoding='utf-8') as f:
        json.dump(brief.model_dump(mode='json'), f, indent=2)
    print('Exported artifacts/escalation_brief_exceeds_approval.json')

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>TRACE — Escalation Brief ({brief.plan_id})</title>
  <style>
    @page {{ size: A4; margin: 15mm; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #111; line-height: 1.4; padding: 20px; max-width: 800px; margin: 0 auto; }}
    .header {{ border-bottom: 2px solid #111; padding-bottom: 10px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: flex-end; }}
    .brand {{ font-size: 1.4rem; font-weight: bold; letter-spacing: -0.5px; }}
    .tag {{ background: #000; color: #fff; padding: 2px 8px; font-size: 0.75rem; font-weight: bold; border-radius: 3px; }}
    .verdict-box {{ border: 2px solid #da3633; background: #fff5f5; padding: 14px; border-radius: 6px; margin-bottom: 16px; }}
    .verdict-title {{ font-size: 1.2rem; font-weight: bold; color: #da3633; display: flex; justify-content: space-between; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; font-size: 0.85rem; margin-top: 8px; padding-top: 8px; border-top: 1px solid #ffcdd2; }}
    section {{ margin-bottom: 16px; }}
    h3 {{ font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-bottom: 8px; color: #333; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-bottom: 10px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
    th {{ background: #f4f4f4; font-weight: bold; }}
    .falsification {{ background: #fff8e1; border-left: 4px solid #ffb300; padding: 10px; font-size: 0.85rem; margin-top: 10px; font-style: italic; }}
    .approval-box {{ border: 1px solid #999; padding: 12px; border-radius: 6px; background: #fafafa; font-size: 0.85rem; display: flex; justify-content: space-between; align-items: center; margin-top: 20px; }}
    .btn-sign {{ border: 1px solid #000; padding: 6px 16px; font-weight: bold; background: #fff; cursor: pointer; }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <div class="brand">TRACE <span class="tag">DISRUPTION CONTROL</span></div>
      <div style="font-size: 0.8rem; color: #666;">Supply Chain Disruption Refusal &amp; Escalation Brief</div>
    </div>
    <div style="text-align: right; font-size: 0.8rem;">
      <div><strong>Date:</strong> {brief.computed_at.isoformat().split('T')[0]}</div>
      <div><strong>Component:</strong> {brief.component_id}</div>
    </div>
  </div>

  <div class="verdict-box">
    <div class="verdict-title">
      <span>DECISION: {brief.decision.upper()}</span>
      <span>Cost: ₹{brief.total_cost:,.2f}</span>
    </div>
    <div style="font-size: 0.85rem; margin-top: 6px;">
      <strong>Triggers Fired:</strong> {', '.join(brief.triggers_fired)} (Plan cost ₹{brief.total_cost:,.2f} exceeds threshold ₹{brief.approval_threshold:,.2f})
    </div>
    <div class="meta-grid">
      <div><strong>Plan ID:</strong> {brief.plan_id}</div>
      <div><strong>Approval Limit:</strong> ₹{brief.approval_threshold:,.2f}</div>
      <div><strong>Model Mode:</strong> {brief.narrated_by}</div>
    </div>
  </div>

  <section>
    <h3>1. Plain-Language Executive Summary</h3>
    <p style="font-size: 0.85rem; white-space: pre-line;">{brief.narration}</p>
  </section>

  <section>
    <h3>2. Recommended Sourcing Combination</h3>
    <table>
      <thead>
        <tr><th>Supplier ID</th><th>Quantity</th><th>Unit Price</th><th>Lead Time</th><th>Reversibility</th></tr>
      </thead>
      <tbody>
"""
    for action in brief.chosen_plan.purchase_actions():
        html_content += f"""
        <tr>
          <td><strong>{action.supplier_id}</strong></td>
          <td>{action.qty} units</td>
          <td>₹{action.unit_price:.2f}</td>
          <td>{action.lead_time_days} days</td>
          <td><span style="text-transform:uppercase; font-size:0.75rem; background:#eee; padding:2px 6px;">{action.reversibility}</span></td>
        </tr>"""

    html_content += """
      </tbody>
    </table>
  </section>

  <section>
    <h3>3. Rejected Alternatives &amp; Quantified Regret</h3>
    <table>
      <thead>
        <tr><th>Option</th><th>Cost Delta / Savings</th><th>Rejection Reason</th><th>Regret Note</th></tr>
      </thead>
      <tbody>
"""
    for alt in brief.rejected_alternatives:
        saved_str = f"+₹{alt.saved:,.2f}" if alt.saved and alt.saved >= 0 else f"-₹{abs(alt.saved):,.2f}" if alt.saved else "N/A"
        html_content += f"""
        <tr>
          <td><strong>{alt.option}</strong></td>
          <td>{saved_str}</td>
          <td>{alt.reason}</td>
          <td>{alt.regret}</td>
        </tr>"""

    html_content += """
      </tbody>
    </table>
  </section>

  <section>
    <h3>4. Inaction Counterfactual (If Refusal Stand Is Maintained)</h3>
    <div style="font-size: 0.85rem; background: #f9f9f9; padding: 10px; border: 1px solid #e0e0e0;">
      <div><strong>Production Orders at Risk:</strong> {len(brief.cost_of_inaction.production_orders_at_risk)} order(s)</div>
      <ul>
"""
    for r in brief.cost_of_inaction.production_orders_at_risk:
        html_content += f"""<li><strong>{r.production_order_id}</strong> ({r.priority} priority): {r.units_unbuilt} units short — {r.inaction_impact}</li>"""

    html_content += f"""
      </ul>
      <div style="margin-top: 6px; font-size: 0.8rem; color: #555;">{brief.cost_of_inaction.baseline_note}</div>
    </div>
  </section>

  <section>
    <h3>5. Falsification Line</h3>
    <div class="falsification">
      🔍 <strong>Falsification Condition:</strong> "{brief.falsification_line}"
    </div>
  </section>

  <div class="approval-box">
    <div>
      <div><strong>Human Approval Signature Required</strong></div>
      <div style="color: #666; font-size: 0.75rem;">POST /agent/approval/{brief.plan_id}</div>
    </div>
    <div>
      <button class="btn-sign">[ ] APPROVE &amp; WRITE ERP</button>
      <button class="btn-sign" style="margin-left: 8px;">[ ] REJECT PLAN</button>
    </div>
  </div>
</body>
</html>
"""

    with open('artifacts/escalation_brief_printable.html', 'w', encoding='utf-8') as f:
        f.write(html_content)
    print('Exported artifacts/escalation_brief_printable.html')

if __name__ == '__main__':
    export_all()
