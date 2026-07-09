"""Production Scheduling Agent — wiring + verifier prechecks.

Mirror of ``agent.py``/``verifier.py`` for the scheduling domain, with one
domain-specific superpower: because the solver is deterministic and lives
in a tool, the deterministic precheck can RE-RUN it and compare. There is
no gray area — either the reported schedule is byte-for-byte a solver
output (same ``solution_id``, same assignments) or it is invented.

Precheck layers:
1. Reproducibility: re-run ``solve_schedule(strategy_used)`` and compare
   ``solution_id`` and the full assignment list.
2. Independent physics: even if reproduction matched, re-validate
   eligibility (line runs the product at the claimed rate), horizon
   bounds, and pinned in-progress orders staying on their line — checked
   against freshly fetched capacities/orders, not against the report.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

from schemas import VerifierIssue
from scheduling_schemas import OrderStatus, ScheduleReport, SchedulingRequest

_HERE = Path(__file__).parent

DEFAULT_MODEL = os.environ.get(
    "SCHEDULING_MODEL", os.environ.get("PROCESS_OPT_MODEL", "anthropic:claude-sonnet-4-6")
)


def load_scheduling_prompt() -> str:
    return (_HERE / "scheduling_prompt.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Toolsets
# --------------------------------------------------------------------------

def scheduling_inprocess() -> MCPToolset:
    from scheduling_mcp_server import mcp as scheduling_server

    return MCPToolset(scheduling_server, id="plant-scheduling")


def scheduling_stdio(python_bin: str = "python") -> MCPToolset:
    transport = StdioTransport(
        command=python_bin,
        args=[str(_HERE / "scheduling_mcp_server.py")],
        cwd=str(_HERE),
    )
    return MCPToolset(transport, id="plant-scheduling")


# --------------------------------------------------------------------------
# Agent factory
# --------------------------------------------------------------------------

def build_scheduling_agent(
    toolset: MCPToolset | None = None,
    model: str = DEFAULT_MODEL,
    memory_toolset: MCPToolset | None = None,
) -> Agent[None, ScheduleReport]:
    toolsets = [toolset or scheduling_inprocess()]
    if memory_toolset is not None:
        toolsets.append(memory_toolset)
    return Agent(
        model,
        name="production-scheduling",
        instructions=load_scheduling_prompt(),
        output_type=ScheduleReport,
        toolsets=toolsets,
        retries=2,
        defer_model_check=True,
    )


def format_scheduling_task(request: SchedulingRequest) -> str:
    return (
        "New scheduling task. Plan and report.\n\n"
        f"```json\n{request.model_dump_json(indent=2)}\n```"
    )


# --------------------------------------------------------------------------
# Deterministic prechecks
# --------------------------------------------------------------------------

def _blocker(description: str) -> VerifierIssue:
    return VerifierIssue(severity="blocker", description=description)


def scheduling_prechecks(report: ScheduleReport) -> list[VerifierIssue]:
    """Re-run the solver and independently re-validate the physics."""
    import scheduling_mcp_server as sched

    issues: list[VerifierIssue] = []
    if report.status == "abstained":
        return issues

    # -- 1. Reproducibility: the schedule must BE a solver output -----------
    strategy = report.strategy_used or "edd"
    rerun = sched.solve_schedule(strategy)
    if not rerun.get("ok"):
        issues.append(_blocker(
            f"could not re-run solver with strategy '{strategy}': "
            f"{rerun.get('error', {}).get('message', 'unknown error')}"
        ))
        return issues

    if report.solution_id != rerun["solution_id"]:
        issues.append(_blocker(
            f"solution_id '{report.solution_id}' does not match the solver's "
            f"'{rerun['solution_id']}' — the reported schedule is not a "
            "reproducible solver output"
        ))

    reported = sorted(
        (a.model_dump(mode="json") for a in report.assignments),
        key=lambda a: a["order_id"],
    )
    solved = sorted(rerun["assignments"], key=lambda a: a["order_id"])
    if reported != solved:
        rep_ids = {a["order_id"] for a in reported}
        sol_ids = {a["order_id"] for a in solved}
        detail = []
        if rep_ids - sol_ids:
            detail.append(f"orders not in solver output: {sorted(rep_ids - sol_ids)}")
        if sol_ids - rep_ids:
            detail.append(f"solver orders missing from report: {sorted(sol_ids - rep_ids)}")
        if not detail:
            detail.append("assignment fields were altered (times/lines/units)")
        issues.append(_blocker(
            "assignments differ from the solver run: " + "; ".join(detail)
        ))

    # -- 2. Independent physics against fresh orders/capacities -------------
    lines = {
        ln["line_id"]: ln for ln in sched.get_line_capacities()["lines"]
    }
    orders = {o["order_id"]: o for o in sched.get_open_orders()["orders"]}

    for a in report.assignments:
        line = lines.get(a.line_id)
        order = orders.get(a.order_id)
        if line is None:
            issues.append(_blocker(f"{a.order_id}: unknown line '{a.line_id}'"))
            continue
        if order is None:
            issues.append(_blocker(f"assignment cites unknown order '{a.order_id}'"))
            continue
        rate = line["rates_units_per_hour"].get(a.product)
        if rate is None:
            issues.append(_blocker(
                f"{a.order_id}: line {a.line_id} is not qualified for product {a.product}"
            ))
            continue
        expected_duration = a.units / rate
        if not math.isclose(a.end_hour - a.start_hour, expected_duration, abs_tol=1e-2):
            issues.append(_blocker(
                f"{a.order_id}: duration {a.end_hour - a.start_hour:.3f}h inconsistent "
                f"with rate {rate} u/h for {a.units} units "
                f"(expected {expected_duration:.3f}h)"
            ))
        if a.end_hour > line["horizon_hours"] + 1e-6:
            issues.append(_blocker(
                f"{a.order_id}: ends at {a.end_hour}h beyond {a.line_id}'s "
                f"horizon of {line['horizon_hours']}h"
            ))
        if (
            order["status"] == OrderStatus.IN_PROGRESS.value
            and order["pinned_line"] != a.line_id
        ):
            issues.append(_blocker(
                f"{a.order_id} is in progress on {order['pinned_line']} but the "
                f"report moves it to {a.line_id}"
            ))

    return issues


if __name__ == "__main__":
    # Full scheduling run against the demo. Requires ANTHROPIC_API_KEY.
    import asyncio

    async def main() -> None:
        agent = build_scheduling_agent()
        request = SchedulingRequest(request_id="smoke-sched-001", horizon_hours=72)
        result = await agent.run(format_scheduling_task(request))
        report = result.output
        print(report.model_dump_json(indent=2))
        issues = scheduling_prechecks(report)
        print("prechecks:", [i.description for i in issues] or "clean")

    asyncio.run(main())
