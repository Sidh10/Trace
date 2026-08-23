# Supply Chain Disruption Control Agent

## Official Hackathon Problem Statement

Build an autonomous agent that prevents production and delivery failures when a supply chain is disrupted. The agent must monitor a simulated procurement and manufacturing environment, detect risks, communicate with suppliers, reason through trade-offs, replan procurement decisions, update operational systems, and escalate to humans only when approval or business risk requires it.

This is not a chatbot, dashboard, document assistant, or fixed automation workflow. The expected solution is an agentic operations controller that can act over multiple steps, recover from uncertainty, and produce a verifiable decision trail.

---

## 1. Problem

Manufacturing, logistics, and retail companies lose significant time and money when suppliers delay shipments, component inventory runs low, prices change suddenly, or production schedules shift unexpectedly.

In many organizations, supply-chain teams still manage these disruptions manually across email, spreadsheets, ERP systems, vendor portals, phone calls, and internal approvals. A buyer may spend hours checking whether a shipment is delayed, asking suppliers for revised dates, comparing alternate vendors, calculating whether production will stop, and requesting approval for emergency purchasing.

The problem becomes harder because information is often incomplete or unreliable. Inventory data may be stale. Suppliers may give vague or overly optimistic replies. The cheapest vendor may fail quality requirements. The fastest vendor may exceed budget. A delayed low-cost shipment may be acceptable for one production order but catastrophic for another.

The challenge is to build an autonomous agent that can control this disruption loop end to end.

---

## 2. Background / Context

Supply-chain operations are dynamic, uncertain, and highly dependent on timely decisions. Traditional ERP and procurement systems are good at storing records, but they are usually weak at autonomous reasoning and action.

Most existing systems can show alerts such as:

- Purchase order delayed
- Inventory below safety stock
- Supplier response pending
- Budget approval required
- Production schedule at risk

However, they usually do not decide what to do next. Human planners must interpret the situation, contact suppliers, compare options, update records, and coordinate recovery.

Recent enterprise AI trends show strong demand for vertical AI agents that operate across email, ERP, spreadsheets, supplier systems, and internal approval workflows. In supply chain specifically, the startup opportunity is not just extracting data from purchase orders. The larger opportunity is building autonomous operators that keep business operations running despite uncertainty.

This problem captures that real-world need in a simulated, hackathon-safe environment.

---

## 3. Objective

Build a **Supply Chain Disruption Control Agent** that keeps production running during simulated supplier and inventory disruptions.

The agent should autonomously:

1. Monitor purchase orders, inventory, supplier replies, and production schedules.
2. Detect when a disruption threatens production or delivery commitments.
3. Investigate the disruption using available tools and data sources.
4. Contact suppliers or request alternate quotes when needed.
5. Compare possible recovery actions across cost, lead time, reliability, quality, and approval constraints.
6. Choose and execute a recovery plan where permitted.
7. Escalate to a human when business rules require approval.
8. Update simulated ERP records.
9. Produce an audit trail explaining what happened, what was considered, what was chosen, and why.

The final system should not merely recommend generic actions. It should actively manage a disruption scenario through multiple tool calls and decisions.

---

## 4. Agent Capabilities

The submitted agent should demonstrate the following capabilities.

### 4.1 Autonomous Monitoring

The agent should continuously or repeatedly inspect the simulated environment for operational risk.

It should monitor:

- Current inventory levels
- Daily component usage
- Safety stock thresholds
- Open purchase orders
- Supplier delivery dates
- Supplier messages
- Production schedules
- Budget limits
- Approval thresholds

The agent should identify not only obvious alerts, but also downstream consequences. For example, a shipment delayed by five days may be acceptable for a low-priority product but dangerous for a high-priority production order.

### 4.2 Dynamic Task Decomposition

The agent should break a disruption into smaller tasks without being hardcoded for one scenario.

Example decomposition:

1. Identify affected component.
2. Check usable inventory.
3. Estimate days of production coverage.
4. Check delayed purchase order.
5. Ask original supplier for revised delivery date.
6. Search alternate suppliers.
7. Request quotes.
8. Compare recovery options.
9. Check budget and approval rules.
10. Create or recommend updated procurement plan.

### 4.3 Tool Selection

The agent should decide which tool to use based on the current state.

It should not call every tool blindly. For example:

- Use inventory tools when stock risk is unclear.
- Use supplier communication when delivery status is uncertain.
- Use RFQ tools when current supply cannot meet demand.
- Use approval tools when a decision crosses budget limits.
- Use ERP update tools only after deciding on an action.

### 4.4 Supplier Communication

The agent should communicate with simulated suppliers through email or API-like tools.

It may need to:

- Ask for updated shipment status.
- Request expedited delivery.
- Ask for partial shipment availability.
- Request quotes from alternate vendors.
- Confirm quality certifications.
- Challenge vague or contradictory supplier replies.

### 4.5 Inventory and Production Reasoning

The agent should calculate whether current stock can support planned production.

It should reason about:

- Current stock
- Usable stock
- Safety stock
- Daily consumption rate
- Production priority
- Deadline risk
- Component criticality
- Possible production rescheduling

### 4.6 Supplier and RFQ Evaluation

When alternate sourcing is needed, the agent should compare suppliers using multiple dimensions:

- Unit price
- Lead time
- Reliability score
- Quality score
- Minimum order quantity
- Available quantity
- Past performance
- Shipping options
- Approval requirements

The best decision may involve splitting an order across suppliers instead of choosing a single vendor.

### 4.7 Constraint and Resource Management

The agent must operate under real business constraints:

- Limited procurement budget
- Approval thresholds
- Quality requirements
- Supplier minimum order quantities
- Production deadlines
- Inventory safety stock
- Delivery lead times
- Limited tool-call budget
- Simulated time pressure

### 4.8 Replanning and Recovery

The environment should be allowed to change while the agent is working.

The agent should adapt when:

- Supplier replies contradict earlier promises.
- Inventory data is corrected.
- Demand suddenly increases.
- Expedited shipping becomes unavailable.
- A supplier rejects the requested quantity.
- A cheaper supplier fails quality checks.

### 4.9 Human Escalation

The agent should not escalate everything. It should escalate only when required.

Escalation may be necessary when:

- Cost exceeds approval threshold.
- No supplier can meet the required deadline.
- Quality risk is high.
- Production shutdown is unavoidable.
- Multiple options have serious business trade-offs.

The escalation should include a concise decision brief, not a vague alert.

### 4.10 Audit Trail

The agent must maintain a readable audit trail.

The audit trail should include:

- Detected disruption
- Data sources checked
- Supplier messages sent and received
- Alternatives considered
- Calculations performed
- Decision made
- Reason for decision
- ERP updates made
- Escalations triggered
- Remaining risks

---

## 5. Available Tools / Data

Participants should be given a simulated supply-chain sandbox. The exact implementation can vary, but the environment should include the following tools or data sources.

### 5.1 Inventory Database

Stores current component inventory.

Example record:

```json
{
  "component_id": "COMP-104",
  "name": "Motor Driver IC",
  "current_stock": 420,
  "usable_stock": 390,
  "daily_usage": 90,
  "safety_stock": 150,
  "warehouse": "Pune-Plant-1",
  "last_updated": "2026-09-01T10:00:00Z"
}
```

### 5.2 Purchase Order System

Stores open and historical purchase orders.

Example record:

```json
{
  "po_id": "PO-7712",
  "component_id": "COMP-104",
  "supplier_id": "SUP-21",
  "quantity": 1000,
  "expected_delivery": "2026-09-04",
  "status": "in_transit",
  "unit_price": 118,
  "total_value": 118000,
  "approval_required_above": 150000
}
```

### 5.3 Supplier Catalog

Contains available suppliers and their capabilities.

Example record:

```json
{
  "supplier_id": "SUP-42",
  "supplier_name": "Western Components Ltd",
  "component_id": "COMP-104",
  "unit_price": 132,
  "lead_time_days": 4,
  "available_quantity": 700,
  "quality_score": 0.94,
  "reliability_score": 0.81,
  "min_order_quantity": 300,
  "certifications": ["ISO-9001", "Automotive-Grade"]
}
```

### 5.4 Production Schedule

Shows production orders and component needs.

Example record:

```json
{
  "production_order_id": "PROD-882",
  "product": "Smart Controller Unit",
  "required_component": "COMP-104",
  "units_planned": 700,
  "component_required_per_unit": 1,
  "deadline": "2026-09-06",
  "priority": "high"
}
```

### 5.5 Simulated Email Inbox

Contains supplier communications.

Example message:

```text
From: supplier21@example.com
Subject: Delay on PO-7712

Due to transport issues, delivery may be delayed by 5-7 days. We are trying to resolve this and will update soon.
```

### 5.6 Supplier Communication Tool

Allows the agent to send supplier messages.

Example request:

```json
{
  "to": "supplier42@example.com",
  "subject": "Urgent RFQ for COMP-104",
  "body": "Can you confirm availability of 600 units of COMP-104 within 4 days, including final price and shipping option?"
}
```

### 5.7 RFQ Tool

Returns supplier quotes based on requested component, quantity, and delivery date.

Example response:

```json
{
  "supplier_id": "SUP-42",
  "component_id": "COMP-104",
  "quantity_available": 600,
  "unit_price": 136,
  "delivery_days": 4,
  "expedite_available": true,
  "expedite_fee": 12000,
  "quote_valid_hours": 6
}
```

### 5.8 Budget and Approval Tool

Checks whether the agent may proceed autonomously.

Example response:

```json
{
  "action": "create_emergency_po",
  "estimated_cost": 168000,
  "approval_required": true,
  "approval_reason": "Cost exceeds autonomous purchase threshold of 150000"
}
```

### 5.9 ERP Update Tool

Allows the agent to update operational records.

Possible actions:

- Mark purchase order as delayed
- Create alternate purchase order
- Attach supplier communication notes
- Update production risk status
- Record escalation request
- Store final recovery plan

### 5.10 Tracking or Verification Tool

Optional but recommended.

This tool lets the agent verify whether supplier claims match shipment or tracking data.

Example:

```json
{
  "po_id": "PO-7712",
  "supplier_claim": "dispatched",
  "tracking_status": "label_created_no_pickup",
  "last_movement": null
}
```

---

## 6. Constraints

The agent must operate under realistic constraints.

### Business Constraints

- Total emergency procurement budget is limited.
- Purchases above a threshold require human approval.
- Some components require certified suppliers.
- Safety stock cannot be consumed without justification.
- Production deadlines vary by priority.
- Some orders can be delayed, while others cannot.

### Supplier Constraints

- Suppliers may have minimum order quantities.
- Suppliers may not have enough available stock.
- Fast delivery may cost more.
- Supplier reliability varies.
- Supplier replies may be delayed, vague, or contradictory.
- Some suppliers may make inaccurate claims.

### System Constraints

- Inventory data may be stale.
- Tool calls may be limited.
- The agent may have incomplete information.
- The simulation may inject new disruptions during execution.
- The final answer must include an audit trail.

### Decision Constraints

The agent should not optimize for only one metric. It must balance:

- Continuity vs cost
- Speed vs quality
- Autonomy vs approval risk
- Supplier reliability vs availability
- Short-term recovery vs long-term supplier risk

---

## 7. Evaluation Criteria

The recommended judging rubric is:

| Category | Weight | What Judges Evaluate |
|---|---:|---|
| Production Continuity | 35% | Did the agent prevent or reduce production stoppage? |
| Cost Control | 20% | Did the agent avoid unnecessary spending while still solving the disruption? |
| Supplier Risk Handling | 15% | Did the agent evaluate reliability, quality, contradictions, and supplier uncertainty? |
| Tool Efficiency | 10% | Did the agent use tools deliberately instead of making wasteful or irrelevant calls? |
| Recovery and Replanning | 10% | Did the agent adapt when new information invalidated its earlier plan? |
| Audit Trail and Explainability | 10% | Are the decisions traceable, justified, and understandable to an operations manager? |

### Suggested Score Outputs

Organizers may calculate a final score using:

```text
Final Score =
  0.35 * Production Continuity Score +
  0.20 * Cost Control Score +
  0.15 * Supplier Risk Score +
  0.10 * Tool Efficiency Score +
  0.10 * Recovery Score +
  0.10 * Audit Trail Score
```

### Hidden Evaluation Tests

To prevent hardcoded solutions, organizers should include hidden disruptions such as:

- A supplier delays delivery after initially confirming availability.
- ERP inventory shows more stock than the warehouse actually has.
- The cheapest supplier fails quality requirements.
- A high-reliability supplier has insufficient quantity.
- A low-reliability supplier offers the fastest delivery.
- A sudden demand spike increases component usage.
- Expedited delivery becomes unavailable.
- A supplier claims dispatch, but tracking data contradicts it.
- A purchase exceeds autonomous approval limits.
- A production order priority changes mid-simulation.

The strongest agents should continue operating correctly under these hidden changes.

---

## 8. Example Scenarios

### Scenario 1: Normal Disruption

Supplier SUP-21 informs the company that PO-7712 for COMP-104 will be delayed by 5 days.

Expected agent behavior:

- Check current inventory.
- Calculate days of production coverage.
- Identify affected production orders.
- Ask SUP-21 for revised delivery confirmation.
- Search alternate suppliers.
- Compare price, lead time, quality, and reliability.
- Recommend or create a recovery order.
- Update ERP with risk status and action taken.

### Scenario 2: Stale Inventory Data

The ERP reports 800 units of COMP-104, but a warehouse update reveals that only 390 are usable.

Expected agent behavior:

- Detect the inventory mismatch.
- Recalculate production coverage.
- Increase risk severity.
- Replan procurement based on usable stock.
- Record the discrepancy in the audit trail.

### Scenario 3: Adversarial Supplier Claim

Supplier SUP-21 claims the delayed shipment has been dispatched, but the tracking system shows only a label was created and no pickup occurred.

Expected agent behavior:

- Avoid trusting the supplier claim blindly.
- Verify shipment status using tracking data.
- Mark supplier reliability risk.
- Continue alternate sourcing.
- Explain why the original supplier was not trusted.

### Scenario 4: Quality Constraint

The cheapest alternate supplier can deliver within 3 days but does not meet the required certification level.

Expected agent behavior:

- Reject the supplier or escalate with risk clearly stated.
- Prefer a certified supplier even if slightly more expensive.
- Explain the quality constraint.

### Scenario 5: Budget Approval Required

The only feasible recovery plan costs more than the autonomous approval threshold.

Expected agent behavior:

- Prepare an approval brief.
- Include cost, production impact, alternatives considered, and risk of no action.
- Avoid executing the purchase without approval.

### Scenario 6: High-Pressure Production Risk

A production line will stop in 12 simulated hours unless the agent secures partial stock or reschedules production.

Expected agent behavior:

- Prioritize critical production orders.
- Consider partial shipments.
- Split orders across suppliers if needed.
- Delay lower-priority production if necessary.
- Produce a time-sensitive recovery plan.

---

## 9. Startup Opportunity

### Target Customers

- Manufacturing companies
- Electronics assemblers
- Automotive suppliers
- Industrial equipment manufacturers
- D2C brands
- Retail distribution networks
- Logistics operators
- Procurement-heavy enterprises

### Buyer Personas

- Chief Operating Officer
- Head of Procurement
- Supply Chain Director
- Plant Manager
- Logistics Operations Head
- Finance Controller for procurement-heavy businesses

### Pain Point

Supply-chain teams spend large amounts of time manually coordinating disruptions. They check ERP systems, chase suppliers, compare spreadsheets, request approvals, and update internal stakeholders.

The cost of delay can be much larger than the cost of the missing component. A small part arriving late can stop a production line, delay customer shipments, or force expensive emergency procurement.

### MVP Startup Wedge

A strong commercial MVP could begin as a **PO expediting and supplier follow-up agent**.

Initial product:

- Monitors open purchase orders.
- Detects late or risky deliveries.
- Emails suppliers automatically.
- Summarizes risk to procurement teams.
- Suggests alternate suppliers.
- Creates approval-ready recovery plans.

### Larger Product Vision

The larger product is an **agentic supply-chain operating layer** that sits above ERP, email, spreadsheets, supplier portals, and logistics systems.

It would continuously manage:

- Supplier risk
- Procurement delays
- Inventory shortages
- Alternate sourcing
- Production continuity
- Approval workflows
- Vendor performance memory

### Defensibility

This product can become defensible through:

- Historical supplier performance data
- ERP and procurement integrations
- Company-specific approval rules
- Learned supplier communication patterns
- Operational memory across disruptions
- Workflow-specific tuning for manufacturing, logistics, or retail

---

## 10. Possible MVP

A credible hackathon MVP should include the following components.

### Core MVP Requirements

1. Simulated ERP database
2. Inventory table
3. Purchase order table
4. Supplier catalog
5. Production schedule
6. Supplier email or message simulator
7. Agent planning loop
8. Tool-calling interface
9. Supplier quote comparison
10. Recovery decision engine
11. Approval escalation logic
12. Audit log
13. Final dashboard or report

### Minimum Demo Flow

The demo should show the following sequence:

1. A supplier delay is injected.
2. The agent detects the disruption.
3. The agent checks inventory and production impact.
4. The agent contacts the original supplier.
5. The agent requests quotes from alternate suppliers.
6. The agent compares options.
7. The agent chooses a recovery plan or escalates for approval.
8. The agent updates the simulated ERP.
9. The agent produces an audit trail.

### Strong MVP Features

Teams can score higher by adding:

- Supplier reliability memory
- Multi-step replanning
- Production rescheduling
- Partial shipment strategy
- Budget-aware optimization
- Adversarial supplier handling
- Human approval workflow
- Visual dashboard
- Simulation replay
- Tool-call trace viewer

---

## 11. Difficulty / Expected Build Time

Expected serious build time: **16-20 hours** for a credible MVP.

Suggested time breakdown:

| Work Area | Estimated Time |
|---|---:|
| Simulated data model and APIs | 3-4 hours |
| Agent planning and state management | 3-4 hours |
| Supplier communication simulator | 2-3 hours |
| Inventory and production-risk logic | 2-3 hours |
| Supplier comparison and optimization | 2-3 hours |
| Failure handling and replanning | 2-3 hours |
| Dashboard, audit trail, or reporting | 2-3 hours |
| Testing and demo preparation | 1-2 hours |

This is intentionally difficult because the agent must reason over a changing environment instead of executing a fixed script.

---

## 12. Layer 1 / Layer 2 / Layer 3 Depth

### Layer 1: Supply-Chain Monitoring Assistant

At this level, the system can:

- Read purchase orders.
- Detect delayed shipments.
- Summarize inventory risk.
- Draft supplier follow-up messages.
- Produce a basic recommendation.

This is useful but not deeply agentic unless it acts and adapts.

### Layer 2: Autonomous Procurement Planner

At this level, the agent can:

- Contact suppliers.
- Request alternate quotes.
- Compare recovery options.
- Check approval thresholds.
- Update ERP records.
- Produce an auditable recovery plan.

This is the expected level for strong hackathon submissions.

### Layer 3: Disruption Control Agent

At this level, the agent can:

- Handle multiple simultaneous disruptions.
- Replan when supplier or inventory assumptions fail.
- Split orders across suppliers.
- Prioritize production schedules.
- Detect misleading supplier claims.
- Maintain supplier performance memory.
- Escalate only when required.
- Optimize continuity, cost, quality, and risk together.

This is the target level for winning submissions.

---

## 13. Existing Market / Competitive Landscape

The market around procurement and supply-chain AI is active and growing.

Relevant categories include:

- Procurement automation
- Supplier relationship management
- ERP workflow automation
- AI operations agents
- Logistics coordination platforms
- Autonomous back-office agents

Large enterprise incumbents include:

- SAP
- Oracle
- Coupa
- Zip
- Traditional ERP and procurement suites

Newer AI-native companies are building agents for procurement, supplier communication, ERP workflows, and back-office operations. The space is becoming competitive, but there is still room for focused products that solve painful operational loops better than broad enterprise platforms.

The differentiation for this problem is **autonomous disruption control**. The agent is not just storing purchase orders or summarizing supplier emails. It is actively trying to prevent operational failure.

---

## 14. Why This Is Genuinely Agentic

This problem is genuinely agentic because the correct solution cannot be built as a simple deterministic workflow.

The agent must:

- Plan independently.
- Decompose a disruption into subproblems.
- Select tools based on context.
- Maintain state across multiple steps.
- Remember supplier behavior.
- Interpret uncertain and conflicting information.
- Balance competing business objectives.
- Take actions in the environment.
- Verify claims before trusting them.
- Recover when earlier assumptions fail.
- Replan when new events occur.
- Escalate only when needed.
- Explain its reasoning clearly.

A chatbot that answers questions about inventory is not sufficient. A script that always chooses the cheapest supplier is not sufficient. A dashboard that only shows alerts is not sufficient.

The challenge requires an autonomous decision-making loop.

---

## 15. Why This Is Suitable for Hackers Occupied Pune

This problem fits Hackers Occupied Pune because it is serious, technical, startup-grade, and relevant to real industry.

Pune has a strong ecosystem around manufacturing, automotive, logistics, enterprise software, and engineering operations. A supply-chain disruption agent connects directly to these domains while still requiring modern agentic AI capabilities.

The problem is suitable for HOP because it requires:

- Multi-tool agent execution
- Long-horizon planning
- State and memory
- Business reasoning
- Simulation design
- Failure recovery
- Human-in-the-loop decision-making
- Measurable evaluation
- Startup thinking

The best submissions will not look like generic AI demos. They will look like autonomous operations systems that could become real enterprise products.

---

## 16. Recommended Organizer Setup

To make this problem effective in a hackathon, organizers should provide a lightweight simulation environment.

### Suggested Dataset Size

- 20-50 components
- 10-20 suppliers
- 20-40 purchase orders
- 5-10 production orders
- 10-20 supplier messages
- 5-8 disruption events
- 3-5 hidden evaluation events

### Suggested APIs

Organizers may expose simple REST APIs or local function tools:

```text
GET /inventory
GET /inventory/{component_id}
GET /purchase-orders
GET /purchase-orders/{po_id}
GET /suppliers?component_id=COMP-104
POST /suppliers/{supplier_id}/message
POST /rfq
POST /approval/check
POST /erp/update
GET /production-schedule
GET /tracking/{po_id}
```

### Suggested Final Submission Artifacts

Each team should submit:

- Working demo
- Source code
- Short architecture explanation
- Agent workflow diagram
- Sample run logs
- Final audit trail from one disruption scenario
- Explanation of how the agent handles hidden failures

---

## 17. Example Final Agent Output

A strong agent may produce a final report like this:

```text
Disruption Detected:
PO-7712 for COMP-104 delayed by 5-7 days. Current usable stock supports 4.3 days of production, but high-priority order PROD-882 requires 700 units by Sept 6.

Actions Taken:
1. Verified current usable inventory from warehouse data.
2. Contacted original supplier SUP-21 for revised delivery confirmation.
3. Checked tracking data and found no shipment movement.
4. Requested RFQs from SUP-42, SUP-37, and SUP-18.
5. Rejected SUP-18 because quality score was below required threshold.
6. Selected split recovery plan using SUP-42 and SUP-37.
7. Checked approval requirement. Total cost requires manager approval.

Recommended Plan:
Approve emergency purchase of 600 units from SUP-42 with 4-day delivery and 300 units from SUP-37 with 6-day delivery. Use current stock for the first 4 days of PROD-882. Delay low-priority PROD-914 by 2 days to preserve safety stock.

Reasoning:
This plan prevents shutdown of the high-priority production order, avoids uncertified suppliers, reduces dependence on the delayed shipment, and keeps cost increase within 12 percent of the baseline plan.

Escalation Required:
Yes. Emergency purchase total exceeds autonomous approval threshold by 18,000.

Remaining Risk:
SUP-42 reliability score is moderate. Agent will recheck confirmation after 6 simulated hours.
```

---

## 18. Safety and Scope Boundaries

This challenge should remain fully simulated.

Participants should not connect their agent to real suppliers, real ERP systems, real email accounts, or real payment systems during the hackathon. All supplier messages, purchase orders, approvals, and ERP updates should occur inside the sandbox.

The agent should be evaluated on reasoning, planning, adaptation, and operational control, not on real-world procurement execution.

---

## 19. One-Line Challenge Summary

Build an autonomous supply-chain operations agent that detects supplier disruptions, reasons across inventory and production risk, negotiates or sources alternatives, updates simulated ERP systems, and keeps production running under uncertainty.
