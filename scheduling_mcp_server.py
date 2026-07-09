"""Scheduling MCP server — the solver lives inside the tool.

The LLM never computes a schedule. ``solve_schedule`` runs a deterministic
EDD (earliest-due-date) greedy solver with capacity and eligibility
constraints, and returns assignments tagged with a ``solution_id`` — a
content hash of the solution. Because the demo data and the algorithm are
deterministic, anyone (the verifier, an auditor, a human) can re-run the
solver and reproduce the exact same ``solution_id``. A schedule that no
solver run can reproduce is, by definition, invented.

Demo dataset: two lines, seven orders, one pinned in-progress order, one
order with no eligible line (exercises the ``unscheduled`` path), and
tight due dates that force at least one late order (exercises the human
approval gate).
"""

from __future__ import annotations

import hashlib
import json

from mcp.server.fastmcp import FastMCP

from scheduling_schemas import (
    LineCapacity,
    OrderStatus,
    ProductionOrder,
    ScheduleAssignment,
    ScheduleMetrics,
    UnscheduledOrder,
)

mcp = FastMCP("plant-scheduling")

# --------------------------------------------------------------------------
# Demo data (deterministic; swap for an ERP/MES adapter in production,
# same pattern as HistorianBackend)
# --------------------------------------------------------------------------

DEMO_LINES: list[LineCapacity] = [
    LineCapacity(
        line_id="L1",
        horizon_hours=72.0,
        rates_units_per_hour={"A": 50.0, "B": 40.0},
    ),
    LineCapacity(
        line_id="L2",
        horizon_hours=72.0,
        rates_units_per_hour={"A": 45.0, "C": 60.0},
    ),
]

DEMO_ORDERS: list[ProductionOrder] = [
    ProductionOrder(order_id="O-1006", product="A", quantity_units=500,
                    due_in_hours=10, status=OrderStatus.IN_PROGRESS,
                    pinned_line="L1"),
    ProductionOrder(order_id="O-1001", product="A", quantity_units=2000,
                    due_in_hours=24),
    ProductionOrder(order_id="O-1003", product="C", quantity_units=1800,
                    due_in_hours=20),
    ProductionOrder(order_id="O-1002", product="B", quantity_units=1200,
                    due_in_hours=30),
    ProductionOrder(order_id="O-1004", product="A", quantity_units=3000,
                    due_in_hours=60),
    ProductionOrder(order_id="O-1005", product="B", quantity_units=800,
                    due_in_hours=70),
    ProductionOrder(order_id="O-1007", product="D", quantity_units=400,
                    due_in_hours=48),  # no line runs product D
]


# --------------------------------------------------------------------------
# Deterministic EDD solver
# --------------------------------------------------------------------------

def _solve_edd(
    orders: list[ProductionOrder], lines: list[LineCapacity]
) -> tuple[list[ScheduleAssignment], list[UnscheduledOrder], ScheduleMetrics, str]:
    cursor: dict[str, float] = {ln.line_id: 0.0 for ln in lines}
    horizon: dict[str, float] = {ln.line_id: ln.horizon_hours for ln in lines}
    rates: dict[str, dict[str, float]] = {
        ln.line_id: ln.rates_units_per_hour for ln in lines
    }

    assignments: list[ScheduleAssignment] = []
    unscheduled: list[UnscheduledOrder] = []

    def place(order: ProductionOrder, line_id: str) -> ScheduleAssignment:
        rate = rates[line_id][order.product]
        start = cursor[line_id]
        end = start + order.quantity_units / rate
        cursor[line_id] = end
        return ScheduleAssignment(
            order_id=order.order_id,
            line_id=line_id,
            product=order.product,
            units=order.quantity_units,
            start_hour=round(start, 3),
            end_hour=round(end, 3),
            due_hour=order.due_in_hours,
            lateness_hours=round(max(0.0, end - order.due_in_hours), 3),
        )

    # 1. Pinned in-progress orders first, on their own line, at the cursor.
    pinned = [o for o in orders if o.status == OrderStatus.IN_PROGRESS]
    for order in sorted(pinned, key=lambda o: (o.due_in_hours, o.order_id)):
        line_id = order.pinned_line
        if line_id not in rates or order.product not in rates[line_id]:
            unscheduled.append(UnscheduledOrder(
                order_id=order.order_id,
                reason=f"pinned line '{line_id}' cannot run product {order.product}",
            ))
            continue
        assignments.append(place(order, line_id))

    # 2. Open orders by earliest due date; greedy earliest-completion line.
    open_orders = [o for o in orders if o.status == OrderStatus.OPEN]
    for order in sorted(open_orders, key=lambda o: (o.due_in_hours, o.order_id)):
        eligible = [
            lid for lid in rates
            if order.product in rates[lid]
            and cursor[lid] + order.quantity_units / rates[lid][order.product]
            <= horizon[lid] + 1e-9
        ]
        if not eligible:
            has_any_line = any(order.product in r for r in rates.values())
            reason = (
                "insufficient remaining capacity within the horizon"
                if has_any_line
                else f"no line is able to run product {order.product}"
            )
            unscheduled.append(UnscheduledOrder(order_id=order.order_id, reason=reason))
            continue
        best = min(
            eligible,
            key=lambda lid: (cursor[lid] + order.quantity_units / rates[lid][order.product], lid),
        )
        assignments.append(place(order, best))

    late = [a for a in assignments if a.lateness_hours > 0]
    metrics = ScheduleMetrics(
        total_orders=len(orders),
        scheduled=len(assignments),
        unscheduled=len(unscheduled),
        late_orders=len(late),
        max_lateness_hours=round(max((a.lateness_hours for a in late), default=0.0), 3),
        utilization={
            lid: round(min(1.0, cursor[lid] / horizon[lid]), 4) for lid in cursor
        },
    )
    payload = json.dumps(
        {
            "strategy": "edd",
            "assignments": [a.model_dump(mode="json") for a in assignments],
            "unscheduled": [u.model_dump(mode="json") for u in unscheduled],
        },
        sort_keys=True,
    )
    solution_id = "sol::edd::" + hashlib.sha256(payload.encode()).hexdigest()[:12]
    return assignments, unscheduled, metrics, solution_id


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def get_open_orders() -> dict:
    """List production orders in the planning horizon, including pinned
    in-progress work. Due dates are hours relative to horizon start."""
    return {"ok": True, "orders": [o.model_dump(mode="json") for o in DEMO_ORDERS]}


@mcp.tool()
def get_line_capacities() -> dict:
    """List production lines with their horizon and per-product rates.
    A product absent from a line's rates cannot run on that line."""
    return {"ok": True, "lines": [ln.model_dump(mode="json") for ln in DEMO_LINES]}


@mcp.tool()
def solve_schedule(strategy: str = "edd") -> dict:
    """Run the deterministic scheduling solver over current orders and
    capacities. Returns assignments, unscheduled orders, metrics and a
    solution_id that MUST be cited in any schedule report — schedules not
    produced by this tool are invalid by policy.

    Args:
        strategy: Solver strategy. Currently only "edd" (earliest due date).
    """
    if strategy != "edd":
        return {"ok": False, "error": {
            "code": "unknown_strategy",
            "message": f"strategy '{strategy}' not available; use 'edd'",
        }}
    assignments, unscheduled, metrics, solution_id = _solve_edd(DEMO_ORDERS, DEMO_LINES)
    return {
        "ok": True,
        "solution_id": solution_id,
        "strategy": "edd",
        "assignments": [a.model_dump(mode="json") for a in assignments],
        "unscheduled": [u.model_dump(mode="json") for u in unscheduled],
        "metrics": metrics.model_dump(mode="json"),
    }


if __name__ == "__main__":
    mcp.run()
