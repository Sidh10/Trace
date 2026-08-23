"""JS-vs-Python coverage parity — cheap insurance against the duplication
logged in OPEN_ITEMS.md.

`static/index.html`'s coverage board recomputes days-of-coverage client-side
(a real day-by-day depletion trajectory, matching `app/engine/coverage.py`'s
own algorithm) rather than calling a backend endpoint that returns
`compute_coverage()`'s real output — there isn't one. That is a disclosed,
deliberate tradeoff (see OPEN_ITEMS.md), not an oversight, but it means the
two implementations can silently drift apart with nothing to catch it short
of a human staring at both side by side.

This test is that catch. It extracts the frontend's own pure math functions
(`daysBetween` / `trajectory` / `firstDayBelow` / `classify`) VERBATIM from
`static/index.html` — not a hand-copied duplicate that could itself drift —
runs them under Node on the exact same raw data the frontend fetches
(`/production-schedule`, `/inventory`, `/purchase-orders`, `/clock`), and
diffs the result against `compute_coverage()`'s real Python output for every
production order across all nine injectable judge-panel scenarios.

Skips (does not fail) if `node` isn't on PATH — this asserts JS/Python
parity, not that Node itself is installed everywhere this suite runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from app.api.routes import reset_orchestrator_state
from app.audit.provenance import reset_provenance_sequences
from app.engine.coverage import compute_coverage, reset_event_sequence
from app.engine.planner import reset_plan_sequence
from app.environment import seed_data
from app.environment.clock import clock
from app.environment.routes import inject_scenario

SCENARIOS = [
    "supplier_delay",
    "quality_fail",
    "insufficient_qty",
    "low_reliability_fastest",
    "exceeds_approval",
    "stale_erp",
    "demand_spike",
    "expedite_unavailable",
    "priority_change",
]

# Matches the board's own display precision (`.toFixed(1)`) — a real
# algorithmic divergence shows up far larger than this; float noise from
# JS's Date-in-milliseconds vs Python's exact day arithmetic does not.
_TOLERANCE_DAYS = 0.05

_START_MARKER = "const EPS = 1e-9;"
_END_MARKER = "async function fetchCoverageBoard()"


def _extract_js_math_functions() -> str:
    """Pull `daysBetween` / `trajectory` / `firstDayBelow` / `classify`
    straight out of the shipped file, byte for byte — if a future edit
    changes this math, this test runs the NEW version, not a stale copy."""
    source = open("static/index.html", encoding="utf-8").read()
    start = source.index(_START_MARKER)
    end = source.index(_END_MARKER, start)
    assert end > start, "static/index.html's coverage-math block moved or was renamed"
    return source[start:end]


# The per-order combination glue `fetchCoverageBoard` itself does — kept
# separate from the extracted math functions above because it is fetch/DOM
# glue, not the algorithm this test exists to pin. Mirrors that function's
# body as of this session; if it changes shape, update this alongside it.
_NODE_HARNESS = """
function run(payload) {
  const { production_schedule, inventory, purchase_orders, clock_now } = payload;
  const now = new Date(clock_now);
  const invMap = {};
  inventory.forEach(i => invMap[i.component_id] = i);

  const poMap = {};
  purchase_orders.forEach(po => {
    if (po.status === 'pending' || po.status === 'in_transit') {
      if (!poMap[po.component_id]) poMap[po.component_id] = [];
      const arrivalOffset = daysBetween(now, new Date(po.expected_delivery + 'T00:00:00Z'));
      poMap[po.component_id].push([arrivalOffset, po.quantity]);
    }
  });

  const result = {};
  production_schedule.forEach(ord => {
    const inv = invMap[ord.required_component] || {};
    const usableStock = inv.usable_stock || 0;
    const dailyUsage = inv.daily_usage || 0;
    const safetyStock = inv.safety_stock || 0;
    const arrivals = poMap[ord.required_component] || [];

    const traj = trajectory(usableStock, dailyUsage, arrivals);
    const onHandTraj = trajectory(usableStock, dailyUsage, []);
    const safetyDay = firstDayBelow(traj.segments, safetyStock);
    const daysToDeadline = daysBetween(now, new Date(ord.deadline + 'T00:00:00Z'));

    result[ord.production_order_id] = {
      days_of_coverage: traj.depleted_at,
      days_of_coverage_on_hand: onHandTraj.depleted_at,
      status: classify(traj.depleted_at, safetyDay, daysToDeadline),
    };
  });
  return result;
}

const payload = JSON.parse(require('fs').readFileSync(0, 'utf-8'));
process.stdout.write(JSON.stringify(run(payload)));
"""


def _run_js_coverage(raw: dict) -> dict:
    script = _extract_js_math_functions() + _NODE_HARNESS
    proc = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(raw),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _python_truth(store, now) -> dict:
    cov = compute_coverage(store, now=now)
    return {
        r.production_order_id: {
            "days_of_coverage": r.days_of_coverage,
            "days_of_coverage_on_hand": r.days_of_coverage_on_hand,
            "status": r.status,
        }
        for r in cov.results
    }


def _raw_inputs(store, now) -> dict:
    return {
        "production_schedule": [p.model_dump(mode="json") for p in store.list_production_schedule()],
        "inventory": [i.model_dump(mode="json") for i in store.list_inventory()],
        "purchase_orders": [po.model_dump(mode="json") for po in store.list_purchase_orders()],
        "clock_now": now.isoformat(),
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not on PATH")
@pytest.mark.parametrize("scenario_name", SCENARIOS)
def test_js_coverage_board_matches_python_compute_coverage(scenario_name):
    """Inject one scenario, run both implementations against the SAME raw
    data, and diff every field for every production order. A real
    algorithmic divergence (not float noise) fails this loudly and names
    the exact scenario/order/field — see this file's module docstring for
    why this exists instead of a shared endpoint."""
    clock.reset()
    store = seed_data.build_store()
    seed_data.STATE = store
    reset_event_sequence()
    reset_plan_sequence()
    reset_provenance_sequences()
    reset_orchestrator_state()

    inject_scenario(scenario_name)

    now = clock.now()
    python_truth = _python_truth(store, now)
    js_result = _run_js_coverage(_raw_inputs(store, now))

    for order_id, py in python_truth.items():
        assert order_id in js_result, f"{scenario_name}/{order_id}: missing from JS result"
        js = js_result[order_id]

        py_doc = float("inf") if py["days_of_coverage"] == float("inf") else py["days_of_coverage"]
        py_doh = float("inf") if py["days_of_coverage_on_hand"] == float("inf") else py["days_of_coverage_on_hand"]

        assert abs(py_doc - js["days_of_coverage"]) <= _TOLERANCE_DAYS, (
            f"{scenario_name}/{order_id}: days_of_coverage diverged — "
            f"python={py_doc}, js={js['days_of_coverage']}"
        )
        assert abs(py_doh - js["days_of_coverage_on_hand"]) <= _TOLERANCE_DAYS, (
            f"{scenario_name}/{order_id}: days_of_coverage_on_hand diverged — "
            f"python={py_doh}, js={js['days_of_coverage_on_hand']}"
        )
        assert py["status"] == js["status"], (
            f"{scenario_name}/{order_id}: status diverged — "
            f"python={py['status']!r}, js={js['status']!r}"
        )
