"""Supply-Chain Resilience Agent — wiring + verifier prechecks.

Mirror of ``scheduling_agent.py`` for the supply-chain domain, with the same
domain-specific superpower: because ``assess_coverage`` is deterministic and
lives in a tool, the deterministic precheck can RE-RUN it and compare. There
is no gray area — either the reported gaps are byte-for-byte an assessment
output (same ``assessment_id``, same gaps) or they are invented.

Precheck layers:
1. Reproducibility: re-run ``assess_coverage`` and compare ``assessment_id``
   and the full gaps list.
2. Evidence resolution: every ``evidence_ref`` on every risk must resolve
   against freshly fetched tool data, not against the report itself.
3. Defense in depth: any money-costing mitigation must already carry
   ``requires_human_approval=True`` (the schema should make this
   impossible, but the precheck does not trust that alone).
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

from schemas import VerifierIssue
from supply_schemas import SupplyChainReport, _MONEY_MITIGATIONS

_HERE = Path(__file__).parent

DEFAULT_MODEL = os.environ.get(
    "SUPPLY_MODEL", os.environ.get("PROCESS_OPT_MODEL", "anthropic:claude-sonnet-4-6")
)


def load_supply_prompt() -> str:
    return (_HERE / "supply_prompt.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Toolsets
# --------------------------------------------------------------------------

def supply_inprocess() -> MCPToolset:
    from supply_mcp_server import mcp as supply_server

    return MCPToolset(supply_server, id="plant-supply")


def supply_stdio(python_bin: str = "python") -> MCPToolset:
    transport = StdioTransport(
        command=python_bin,
        args=[str(_HERE / "supply_mcp_server.py")],
        cwd=str(_HERE),
    )
    return MCPToolset(transport, id="plant-supply")


# --------------------------------------------------------------------------
# Agent factory
# --------------------------------------------------------------------------

def build_supply_agent(
    toolset: MCPToolset | None = None,
    model: str = DEFAULT_MODEL,
    memory_toolset: MCPToolset | None = None,
) -> Agent[None, SupplyChainReport]:
    toolsets = [toolset or supply_inprocess()]
    if memory_toolset is not None:
        toolsets.append(memory_toolset)
    return Agent(
        model,
        name="supply-chain-resilience",
        instructions=load_supply_prompt(),
        output_type=SupplyChainReport,
        toolsets=toolsets,
        retries=2,
        defer_model_check=True,
    )


def format_supply_task(request_id: str) -> str:
    return (
        "New supply-chain assessment task. Assess coverage and report.\n\n"
        f"request_id: {request_id}"
    )


# --------------------------------------------------------------------------
# Deterministic prechecks
# --------------------------------------------------------------------------

def _blocker(description: str) -> VerifierIssue:
    return VerifierIssue(severity="blocker", description=description)


def supply_prechecks(report: SupplyChainReport) -> list[VerifierIssue]:
    """Re-run the assessment and independently resolve every evidence_ref."""
    import supply_mcp_server as supply

    issues: list[VerifierIssue] = []
    if report.status == "abstained":
        return issues

    # -- 1. Reproducibility: the gaps must BE an assess_coverage output -----
    rerun = supply.assess_coverage()
    if not rerun.get("ok"):
        issues.append(_blocker(
            f"could not re-run assess_coverage: "
            f"{rerun.get('error', {}).get('message', 'unknown error')}"
        ))
        return issues

    if report.assessment_id != rerun["assessment_id"]:
        issues.append(_blocker(
            f"assessment_id '{report.assessment_id}' does not match the tool's "
            f"'{rerun['assessment_id']}' — the reported gaps are not a "
            "reproducible assessment"
        ))

    reported_gaps = sorted(
        (g.model_dump(mode="json") for g in report.gaps),
        key=lambda g: g["gap_id"],
    )
    fresh_gaps = sorted(rerun["gaps"], key=lambda g: g["gap_id"])
    if reported_gaps != fresh_gaps:
        rep_ids = {g["gap_id"] for g in reported_gaps}
        fresh_ids = {g["gap_id"] for g in fresh_gaps}
        detail = []
        if rep_ids - fresh_ids:
            detail.append(f"gaps not in assessment output: {sorted(rep_ids - fresh_ids)}")
        if fresh_ids - rep_ids:
            detail.append(f"assessment gaps missing from report: {sorted(fresh_ids - rep_ids)}")
        if not detail:
            detail.append("gap fields were altered (shortfall/timing/sourcing)")
        issues.append(_blocker(
            "gaps differ from the assessment run: " + "; ".join(detail)
        ))

    # -- 2. Evidence resolution against fresh tool data ----------------------
    known_gap_ids = {g["gap_id"] for g in fresh_gaps}
    known_po_ids = {po["po_id"] for po in supply.get_open_purchase_orders()["purchase_orders"]}
    known_supplier_ids = {s["supplier_id"] for s in supply.get_supplier_profiles()["suppliers"]}
    known_inv_materials = {p["material"] for p in supply.get_inventory()["positions"]}
    known_req_materials = {r["material"] for r in supply.get_material_requirements()["requirements"]}

    for risk in report.risks:
        for ref in risk.evidence_refs:
            if ref.startswith("gap::"):
                if ref not in known_gap_ids:
                    issues.append(_blocker(
                        f"risk {risk.risk_id} cites unknown gap '{ref}'"
                    ))
            elif ref.startswith("po::"):
                if ref[len("po::"):] not in known_po_ids:
                    issues.append(_blocker(
                        f"risk {risk.risk_id} cites unknown purchase order '{ref}'"
                    ))
            elif ref.startswith("supplier::"):
                if ref[len("supplier::"):] not in known_supplier_ids:
                    issues.append(_blocker(
                        f"risk {risk.risk_id} cites unknown supplier '{ref}'"
                    ))
            elif ref.startswith("inv::"):
                if ref[len("inv::"):] not in known_inv_materials:
                    issues.append(_blocker(
                        f"risk {risk.risk_id} cites unknown inventory position '{ref}'"
                    ))
            elif ref.startswith("req::"):
                if ref[len("req::"):] not in known_req_materials:
                    issues.append(_blocker(
                        f"risk {risk.risk_id} cites unknown requirement '{ref}'"
                    ))
            else:
                issues.append(_blocker(
                    f"risk {risk.risk_id} cites evidence_ref with unknown prefix: '{ref}'"
                ))

    # -- 3. Defense in depth: money mitigations must already force the gate --
    for risk in report.risks:
        if risk.mitigation in _MONEY_MITIGATIONS and not report.requires_human_approval:
            issues.append(_blocker(
                f"risk {risk.risk_id} proposes '{risk.mitigation.value}' but "
                "requires_human_approval is False"
            ))

    return issues


if __name__ == "__main__":
    # Full supply-chain run against the demo. Requires ANTHROPIC_API_KEY.
    import asyncio

    async def main() -> None:
        agent = build_supply_agent()
        result = await agent.run(format_supply_task("smoke-supply-001"))
        report = result.output
        print(report.model_dump_json(indent=2))
        issues = supply_prechecks(report)
        print("prechecks:", [i.description for i in issues] or "clean")

    asyncio.run(main())
