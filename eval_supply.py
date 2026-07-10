"""Eval case 07 — the Supply-Chain Resilience specialist cannot invent a gap.

Offline checks:

  1. ASSESSMENT PHYSICS  assess_coverage finds exactly the two planted
                         findings (M-RES gap with correct shortfall;
                         M-ADD fully covered but single-sourced, no gap)
                         and is deterministic (same assessment_id twice).
  2. SCHEMA GATES        gaps without assessment_id fail; a zero-shortfall
                         CoverageGap fails; a money-mitigation report with
                         requires_human_approval=False comes out forced
                         True; dirty abstention (risks attached) fails.
  3. HONEST REPORT       built verbatim from a real assess run -> prechecks
                         clean.
  4. TAMPERED REPORTS    each blocked: a) hand-edited shortfall number,
                         b) fabricated extra gap cited as evidence,
                         c) invented supplier id in evidence_refs.
  5. GHOST SUPPLIER      a PO citing an unknown supplier does not crash the
                         assessment, is excluded from coverage math, and is
                         named in notes.
  6. AGENT PLUMBING      the agent runs end-to-end with TestModel over the
                         real MCP toolset; the human gate is forced through
                         the live loop when the scripted report proposes an
                         expedite.
"""

from __future__ import annotations

import asyncio

import supply_mcp_server as supply
from supply_agent import (
    build_supply_agent,
    format_supply_task,
    supply_inprocess,
    supply_prechecks,
)
from supply_schemas import CoverageGap, PurchaseOrder, SupplyChainReport


def build_report(**overrides) -> SupplyChainReport:
    """Build a report verbatim from a real assess_coverage run, then apply overrides."""
    run = supply.assess_coverage()
    base = dict(
        request_id="eval-supply",
        status="ok",
        assessment_id=run["assessment_id"],
        gaps=run["gaps"],
        risks=[
            {
                "risk_id": "risk::1",
                "description": "M-RES short 600 units for O-1003 due at hour 20; "
                                "sole supplier SUP-1 backs this material.",
                "severity": "high",
                "evidence_refs": ["gap::M-RES::1", "supplier::SUP-1"],
                "mitigation": "expedite_po",
            },
            {
                "risk_id": "risk::2",
                "description": "M-ADD is fully covered but sourced exclusively through "
                                "SUP-3; a single disruption stalls all B-line output.",
                "severity": "medium",
                "evidence_refs": ["supplier::SUP-3"],
                "mitigation": "monitor",
            },
        ],
        summary="M-RES is short 600 units for O-1003 (due hour 20) and needs an "
                "expedite signature; M-ADD is covered but single-sourced through "
                "SUP-3 and should be monitored.",
    )
    base.update(overrides)
    return SupplyChainReport(**base)


def main() -> int:
    failures: list[str] = []

    # 1. Assessment physics ----------------------------------------------------
    run = supply.assess_coverage()
    gap_materials = {g["material"] for g in run["gaps"]}
    m_res_gap = next((g for g in run["gaps"] if g["material"] == "M-RES"), None)
    problems = []
    if gap_materials != {"M-RES"}:
        problems.append(f"unexpected gap materials: {gap_materials}")
    if m_res_gap is None or m_res_gap["shortfall_units"] != 600 or m_res_gap["at_hours"] != 20:
        problems.append(f"M-RES gap malformed: {m_res_gap}")
    if m_res_gap is not None and m_res_gap["contributing_po_ids"] != ["PO-2001"]:
        problems.append(f"M-RES contributing POs wrong: {m_res_gap['contributing_po_ids']}")
    if "M-PEL" in gap_materials or "M-ADD" in gap_materials:
        problems.append("M-PEL/M-ADD should be comfortably covered, not gapped")
    madd_noted = any("M-ADD" in n and "single-sourced" in n for n in run["notes"])
    if not madd_noted:
        problems.append(f"M-ADD single-sourcing not surfaced in notes: {run['notes']}")
    deterministic = supply.assess_coverage()["assessment_id"] == run["assessment_id"]
    if problems or not deterministic:
        failures.append(f"1: assessment physics broken: {problems}, det={deterministic}")
    print("1) física del assess -> hallazgos plantados ✓ | determinista:", deterministic,
          "| gap M-RES:", m_res_gap["shortfall_units"] if m_res_gap else None,
          "| M-ADD single-sourced sin gap:", madd_noted)

    # 2. Schema gates ------------------------------------------------------------
    gate_results = []
    try:  # a) gaps without assessment_id
        build_report(assessment_id=None)
        gate_results.append("sin assessment_id PASÓ (mal)")
    except Exception:
        gate_results.append("sin assessment_id rechazado ✓")
    try:  # b) zero-shortfall gap
        CoverageGap(gap_id="gap::X::1", material="X", shortfall_units=0,
                    at_hours=5, single_sourced=False, contributing_po_ids=[])
        gate_results.append("shortfall cero PASÓ (mal)")
    except Exception:
        gate_results.append("shortfall cero rechazado ✓")
    honest = build_report(requires_human_approval=False)  # model tries to skip gate
    gate_results.append(
        "gate humano forzado ✓" if honest.requires_human_approval
        else "gate humano NO forzado (mal)"
    )
    try:  # d) dirty abstention
        SupplyChainReport(request_id="x", status="abstained", abstain_reason="no data",
                          summary="s", risks=honest.model_dump(mode="json")["risks"])
        gate_results.append("abstención sucia PASÓ (mal)")
    except Exception:
        gate_results.append("abstención sucia rechazada ✓")
    if any("(mal)" in g for g in gate_results):
        failures.append(f"2: schema gates: {gate_results}")
    print("2) gates del schema ->", " | ".join(gate_results))

    # 3. Honest report -> clean prechecks ----------------------------------------
    issues = supply_prechecks(honest)
    if issues:
        failures.append(f"3: honest report flagged: {[i.description for i in issues]}")
    print("3) reporte honesto -> prechecks:", "limpio ✓" if not issues else issues)

    # 4. Tampered reports ----------------------------------------------------------
    # a) Hand-edited shortfall: keep the real gap_id but change the number.
    edited = [dict(g) for g in run["gaps"]]
    edited[0]["shortfall_units"] = 1.0
    issues_a = supply_prechecks(build_report(gaps=edited))
    caught_a = any("differ from the assessment run" in i.description
                   or "does not match" in i.description for i in issues_a)

    # b) Fabricated extra gap (self-consistent schema-wise, absent from the
    #    real assessment) cited as evidence by an extra risk.
    fabricated = [dict(g) for g in run["gaps"]] + [{
        "gap_id": "gap::M-ADD::1", "material": "M-ADD", "shortfall_units": 250.0,
        "at_hours": 30.0, "single_sourced": True, "contributing_po_ids": ["PO-3001"],
    }]
    extra_risk = {
        "risk_id": "risk::3", "description": "M-ADD is short 250 units per this gap.",
        "severity": "high", "evidence_refs": ["gap::M-ADD::1", "supplier::SUP-3"],
        "mitigation": "expedite_po",
    }
    tampered_b = build_report(
        gaps=fabricated,
        risks=[*build_report().model_dump(mode="json")["risks"], extra_risk],
    )
    issues_b = supply_prechecks(tampered_b)
    caught_b = any("differ from the assessment run" in i.description for i in issues_b)

    # c) Invented supplier id in evidence_refs (schema doesn't check supplier::
    #    refs, only gap::, so this only gets caught at the precheck layer).
    ghost_ref_risks = [dict(r) for r in build_report().model_dump(mode="json")["risks"]]
    ghost_ref_risks[1]["evidence_refs"] = ["supplier::SUP-GHOST"]
    issues_c = supply_prechecks(build_report(risks=ghost_ref_risks))
    caught_c = any("unknown supplier" in i.description for i in issues_c)

    if not (caught_a and caught_b and caught_c):
        failures.append(f"4: tampering missed (a={caught_a}, b={caught_b}, c={caught_c})")
    print("4) reportes manipulados -> shortfall editado:", caught_a,
          "| gap fabricado:", caught_b, "| proveedor inventado:", caught_c)

    # 5. Ghost supplier PO --------------------------------------------------------
    # Regression guard: a PO citing a decommissioned/mistyped supplier must not
    # crash the assessment (KeyError-style failure) and must not silently count
    # toward coverage — it belongs in notes with a clear reason.
    ghost_po = PurchaseOrder(po_id="PO-GHOST", material="M-RES", quantity_units=5000.0,
                             supplier_id="SUP-GHOST", promised_in_hours=5.0, status="open")
    original_pos = supply.DEMO_PURCHASE_ORDERS
    supply.DEMO_PURCHASE_ORDERS = [*original_pos, ghost_po]
    ghost_run = None
    ghost_crashed = False
    try:
        ghost_run = supply.assess_coverage()
    except Exception as exc:
        ghost_crashed = True
        failures.append(f"5: assess_coverage crashed on unknown supplier PO: {exc!r}")
    finally:
        supply.DEMO_PURCHASE_ORDERS = original_pos

    ghost_note = None
    if not ghost_crashed:
        ghost_gap = next((g for g in ghost_run["gaps"] if g["material"] == "M-RES"), None)
        ghost_note = next((n for n in ghost_run["notes"] if "PO-GHOST" in n), None)
        if ghost_gap is None or ghost_gap["shortfall_units"] != 600:
            # Negative assertion: if the ghost PO were silently counted, the
            # 5000-unit "supply" would erase the M-RES gap entirely.
            failures.append(f"5: ghost PO affected M-RES coverage: {ghost_gap}")
        if ghost_note is None:
            failures.append("5: ghost PO was not named in notes")
        elif "SUP-GHOST" not in ghost_note:
            failures.append(f"5: note does not name the unknown supplier: {ghost_note!r}")
    print("5) proveedor fantasma -> sin crash:", not ghost_crashed,
          "| nota:", ghost_note if not ghost_crashed else "N/A")

    # 6. Agent plumbing with TestModel --------------------------------------------
    from pydantic_ai.models.test import TestModel

    agent = build_supply_agent(toolset=supply_inprocess())
    payload = build_report(requires_human_approval=False).model_dump(mode="json")

    async def run_agent():
        with agent.override(model=TestModel(custom_output_args=payload)):
            return await agent.run(format_supply_task("eval-supply"))

    result = asyncio.run(run_agent())
    report: SupplyChainReport = result.output
    tools_called = sorted({
        p.tool_name for m in result.all_messages() for p in getattr(m, "parts", [])
        if p.__class__.__name__ == "ToolCallPart" and p.tool_name != "final_result"
    })
    if not report.requires_human_approval:
        failures.append("6: human gate not forced through the live loop")
    print("6) plumbing del agente -> tools:", tools_called,
          "| gate humano en loop real:", report.requires_human_approval)

    if failures:
        print("\nEVAL FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nEVAL PASSED: la evaluación de cobertura respeta la física, el schema "
          "falla cerrado, y ningún hallazgo inventado o manipulado sobrevive a los "
          "prechecks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
