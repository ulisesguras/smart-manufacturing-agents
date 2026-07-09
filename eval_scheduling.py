"""Eval case 06 — the Scheduling specialist cannot invent a plan.

Offline checks:

  1. SOLVER PHYSICS   the EDD solver's output respects eligibility,
                      horizon bounds, non-overlap per line, pinned
                      in-progress orders, and is deterministic.
  2. SCHEMA GATES     overlapping assignments fail validation; a late
                      assignment forces requires_human_approval; a plan
                      without solution_id fails; dirty abstentions fail.
  3. HONEST REPORT    a report built verbatim from a solver run passes
                      prechecks clean.
  4. TAMPERED PLANS   three manipulations are each blocked:
                      a) hand-edited start times (schema-valid, solver-
                         irreproducible),
                      b) an order moved to a non-qualified line,
                      c) a pinned in-progress order moved off its line.
  5. AGENT PLUMBING   the agent runs end-to-end with TestModel over the
                      real MCP toolset and the schema forces the human
                      gate on a late plan.
"""

from __future__ import annotations

import asyncio

import scheduling_mcp_server as sched
from scheduling_agent import (
    build_scheduling_agent,
    format_scheduling_task,
    scheduling_inprocess,
    scheduling_prechecks,
)
from scheduling_schemas import (
    ScheduleMetrics,
    ScheduleReport,
    SchedulingRequest,
)


def solver_report(**overrides) -> ScheduleReport:
    """Build a report verbatim from a real solver run, then apply overrides."""
    run = sched.solve_schedule("edd")
    base = dict(
        request_id="eval-sched",
        status="ok",
        strategy_used="edd",
        solution_id=run["solution_id"],
        assignments=run["assignments"],
        unscheduled=run["unscheduled"],
        metrics=ScheduleMetrics(**run["metrics"]),
        risks=[{"description": "L1 saturated at 97% while L2 idles at 42%",
                "severity": "medium"}],
        summary="Two orders run late and three cannot be scheduled in horizon; "
                "supervisor approval required.",
    )
    base.update(overrides)
    return ScheduleReport(**base)


def main() -> int:
    failures: list[str] = []

    # 1. Solver physics -------------------------------------------------------
    run = sched.solve_schedule("edd")
    lines = {l["line_id"]: l for l in sched.get_line_capacities()["lines"]}
    orders = {o["order_id"]: o for o in sched.get_open_orders()["orders"]}
    problems = []
    by_line: dict[str, list[dict]] = {}
    for a in run["assignments"]:
        by_line.setdefault(a["line_id"], []).append(a)
        if a["product"] not in lines[a["line_id"]]["rates_units_per_hour"]:
            problems.append(f"{a['order_id']} on non-qualified line")
        if a["end_hour"] > lines[a["line_id"]]["horizon_hours"] + 1e-6:
            problems.append(f"{a['order_id']} beyond horizon")
        o = orders[a["order_id"]]
        if o["status"] == "in_progress" and o["pinned_line"] != a["line_id"]:
            problems.append(f"{a['order_id']} unpinned")
    for line_id, items in by_line.items():
        items.sort(key=lambda x: x["start_hour"])
        for p, n in zip(items, items[1:]):
            if n["start_hour"] < p["end_hour"] - 1e-6:
                problems.append(f"overlap on {line_id}")
    deterministic = sched.solve_schedule("edd")["solution_id"] == run["solution_id"]
    if problems or not deterministic:
        failures.append(f"1: solver physics broken: {problems}, det={deterministic}")
    print("1) física del solver -> invariantes ✓ | determinista:", deterministic,
          "| tarde:", run["metrics"]["late_orders"],
          "| sin programar:", run["metrics"]["unscheduled"])

    # 2. Schema gates ----------------------------------------------------------
    gate_results = []
    try:  # a) overlap
        bad = [dict(a) for a in run["assignments"]]
        l1 = [a for a in bad if a["line_id"] == "L1"]
        l1[1]["start_hour"] = l1[0]["start_hour"] + 0.5  # crash into predecessor
        l1[1]["end_hour"] = l1[1]["start_hour"] + 5
        l1[1]["lateness_hours"] = max(0.0, l1[1]["end_hour"] - l1[1]["due_hour"])
        solver_report(assignments=bad)
        gate_results.append("overlap PASÓ (mal)")
    except Exception:
        gate_results.append("overlap rechazado ✓")
    honest = solver_report(requires_human_approval=False)  # model tries to skip gate
    gate_results.append(
        "gate humano forzado ✓" if honest.requires_human_approval
        else "gate humano NO forzado (mal)"
    )
    try:  # c) assignments without solution_id
        solver_report(solution_id=None)
        gate_results.append("sin solution_id PASÓ (mal)")
    except Exception:
        gate_results.append("sin solution_id rechazado ✓")
    try:  # d) dirty abstention
        ScheduleReport(request_id="x", status="abstained",
                       abstain_reason="no data", summary="s",
                       assignments=run["assignments"])
        gate_results.append("abstención sucia PASÓ (mal)")
    except Exception:
        gate_results.append("abstención sucia rechazada ✓")
    if any("(mal)" in g for g in gate_results):
        failures.append(f"2: schema gates: {gate_results}")
    print("2) gates del schema ->", " | ".join(gate_results))

    # 3. Honest report -> clean prechecks ---------------------------------------
    issues = scheduling_prechecks(honest)
    if issues:
        failures.append(f"3: honest report flagged: {[i.description for i in issues]}")
    print("3) reporte honesto -> prechecks:", "limpio ✓" if not issues else issues)

    # 4. Tampered plans ----------------------------------------------------------
    # a) Hand-edited times: shift one assignment 1h later (still schema-valid:
    #    no overlap created on its line, lateness kept consistent).
    edited = [dict(a) for a in run["assignments"]]
    victim = max(edited, key=lambda a: a["start_hour"])  # last on its line
    victim["start_hour"] += 1.0
    victim["end_hour"] += 1.0
    victim["lateness_hours"] = round(max(0.0, victim["end_hour"] - victim["due_hour"]), 3)
    issues_a = scheduling_prechecks(solver_report(assignments=edited))
    caught_a = any("differ from the solver run" in i.description
                   or "does not match" in i.description for i in issues_a)

    # b) Order moved to a non-qualified line (product B onto L2).
    moved = [dict(a) for a in run["assignments"]]
    b_order = next(a for a in moved if a["product"] == "B")
    b_order["line_id"] = "L2"
    issues_b = scheduling_prechecks(solver_report(assignments=moved))
    caught_b = any("not qualified" in i.description for i in issues_b)

    # c) Pinned in-progress order moved off its line (A runs on L2 too,
    #    so eligibility alone would not catch this one — pinning must).
    #    Placed AFTER L2's last job so the schema's overlap gate stays
    #    silent and only the pinning precheck can catch it.
    unpinned = [dict(a) for a in run["assignments"]]
    pinned = next(a for a in unpinned if a["order_id"] == "O-1006")
    l2_tail = max((a["end_hour"] for a in unpinned
                   if a["line_id"] == "L2" and a["order_id"] != "O-1006"),
                  default=0.0)
    rate_l2_a = 45.0
    pinned["line_id"] = "L2"
    pinned["start_hour"] = round(l2_tail, 3)
    pinned["end_hour"] = round(l2_tail + pinned["units"] / rate_l2_a, 3)
    pinned["lateness_hours"] = round(
        max(0.0, pinned["end_hour"] - pinned["due_hour"]), 3
    )
    issues_c = scheduling_prechecks(solver_report(assignments=unpinned))
    caught_c = any("in progress on" in i.description for i in issues_c)

    if not (caught_a and caught_b and caught_c):
        failures.append(f"4: tampering missed (a={caught_a}, b={caught_b}, c={caught_c})")
    print("4) planes manipulados -> tiempos editados:", caught_a,
          "| línea no calificada:", caught_b, "| orden despinneada:", caught_c)

    # 5. Agent plumbing with TestModel -------------------------------------------
    from pydantic_ai.models.test import TestModel

    agent = build_scheduling_agent(toolset=scheduling_inprocess())
    payload = solver_report(requires_human_approval=False).model_dump(mode="json")

    async def run_agent():
        with agent.override(model=TestModel(custom_output_args=payload)):
            return await agent.run(
                format_scheduling_task(SchedulingRequest(request_id="eval-sched",
                                                         horizon_hours=72))
            )

    result = asyncio.run(run_agent())
    report: ScheduleReport = result.output
    tools_called = sorted({
        p.tool_name for m in result.all_messages() for p in getattr(m, "parts", [])
        if p.__class__.__name__ == "ToolCallPart" and p.tool_name != "final_result"
    })
    if not report.requires_human_approval:
        failures.append("5: human gate not forced through the live loop")
    print("5) plumbing del agente -> tools:", tools_called,
          "| gate humano en loop real:", report.requires_human_approval)

    if failures:
        print("\nEVAL FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nEVAL PASSED: el solver respeta la física, el schema falla cerrado, "
          "y ningún cronograma inventado o manipulado sobrevive a los prechecks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
