"""Supply-chain MCP server — coverage math lives inside the tool.

The LLM never computes a shortfall. ``assess_coverage`` runs a deterministic
timeline per material (on-hand minus safety stock, plus purchase orders that
arrive before each need, compared against cumulative demand) and returns
gaps tagged with an ``assessment_id`` — a content hash of the inputs and the
computed gaps. Because the demo data and the algorithm are deterministic,
anyone (the verifier, an auditor, a human) can re-run the assessment and
reproduce the exact same ``assessment_id``. A gap no assessment run can
reproduce is, by definition, invented.

Demand is derived from ``scheduling_mcp_server.DEMO_ORDERS`` (imported, not
redefined) via a bill of materials — the first cross-agent seam: this
agent's demand comes from the same world the scheduler plans.

Demo dataset plants exactly two findings:
  1. an M-RES coverage gap (inventory + inbound < demand from O-1003),
  2. M-ADD fully covered but single-sourced through SUP-3.
Everything else (M-PEL) is comfortably covered.
"""

from __future__ import annotations

import hashlib
import json

from mcp.server.fastmcp import FastMCP

from scheduling_mcp_server import DEMO_ORDERS
from supply_schemas import (
    CoverageGap,
    InventoryPosition,
    MaterialRequirement,
    PurchaseOrder,
    SupplierProfile,
)

mcp = FastMCP("plant-supply")

# --------------------------------------------------------------------------
# Demo data (deterministic; swap for an ERP/MES adapter in production, same
# pattern as HistorianBackend)
# --------------------------------------------------------------------------

# product -> {material -> units of material per unit of product}
BOM: dict[str, dict[str, float]] = {
    "A": {"M-PEL": 2.0},
    "B": {"M-PEL": 1.5, "M-ADD": 0.5},
    "C": {"M-RES": 1.0},
}

DEMO_SUPPLIERS: list[SupplierProfile] = [
    SupplierProfile(supplier_id="SUP-1", name="Alpha Polymers",
                     materials=["M-PEL", "M-RES"], otd_rate=0.95, avg_delay_hours=2.0),
    SupplierProfile(supplier_id="SUP-2", name="Beta Feedstock",
                     materials=["M-PEL"], otd_rate=0.60, avg_delay_hours=24.0),
    SupplierProfile(supplier_id="SUP-3", name="Gamma Additives",
                     materials=["M-ADD"], otd_rate=0.90, avg_delay_hours=4.0),
]

DEMO_INVENTORY: list[InventoryPosition] = [
    InventoryPosition(material="M-PEL", on_hand_units=3000.0, safety_stock_units=500.0),
    InventoryPosition(material="M-ADD", on_hand_units=400.0, safety_stock_units=50.0),
    InventoryPosition(material="M-RES", on_hand_units=600.0, safety_stock_units=100.0),
]

DEMO_PURCHASE_ORDERS: list[PurchaseOrder] = [
    PurchaseOrder(po_id="PO-1001", material="M-PEL", quantity_units=3000.0,
                  supplier_id="SUP-1", promised_in_hours=8.0, status="open"),
    PurchaseOrder(po_id="PO-1002", material="M-PEL", quantity_units=5000.0,
                  supplier_id="SUP-2", promised_in_hours=20.0, status="open"),
    PurchaseOrder(po_id="PO-1003", material="M-PEL", quantity_units=5000.0,
                  supplier_id="SUP-1", promised_in_hours=45.0, status="in_transit"),
    PurchaseOrder(po_id="PO-2001", material="M-RES", quantity_units=700.0,
                  supplier_id="SUP-1", promised_in_hours=18.0, status="open"),
    PurchaseOrder(po_id="PO-3001", material="M-ADD", quantity_units=700.0,
                  supplier_id="SUP-3", promised_in_hours=25.0, status="open"),
]


# --------------------------------------------------------------------------
# Demand: BOM x scheduling orders
# --------------------------------------------------------------------------

def _material_events() -> dict[str, list[tuple[float, float, str]]]:
    """material -> sorted list of (needed_in_hours, required_units, order_id).

    One event per (order, material-in-BOM) pair. Orders whose product has
    no BOM entry (product D) contribute no events — they cannot be built
    regardless of materials, and carry no eligible line either.
    """
    events: dict[str, list[tuple[float, float, str]]] = {}
    for order in DEMO_ORDERS:
        for material, coef in BOM.get(order.product, {}).items():
            events.setdefault(material, []).append(
                (order.due_in_hours, coef * order.quantity_units, order.order_id)
            )
    for material in events:
        events[material].sort(key=lambda e: (e[0], e[2]))
    return events


def _aggregate_requirements() -> list[MaterialRequirement]:
    """One MaterialRequirement per material: summed units, earliest need."""
    out: list[MaterialRequirement] = []
    for material, events in sorted(_material_events().items()):
        out.append(MaterialRequirement(
            material=material,
            required_units=sum(qty for _, qty, _ in events),
            needed_in_hours=min(hours for hours, _, _ in events),
            source_order_ids=sorted({order_id for _, _, order_id in events}),
        ))
    return out


# --------------------------------------------------------------------------
# Deterministic coverage assessment
# --------------------------------------------------------------------------

def _assess() -> tuple[list[CoverageGap], list[str], list[str]]:
    """Returns (gaps, checked_materials, notes)."""
    events_by_material = _material_events()
    inventory_by_material = {p.material: p for p in DEMO_INVENTORY}
    supplier_count: dict[str, int] = {}
    for s in DEMO_SUPPLIERS:
        for m in s.materials:
            supplier_count[m] = supplier_count.get(m, 0) + 1
    known_supplier_ids = {s.supplier_id for s in DEMO_SUPPLIERS}

    notes: list[str] = []
    pos_by_material: dict[str, list[PurchaseOrder]] = {}
    for po in DEMO_PURCHASE_ORDERS:
        if po.supplier_id not in known_supplier_ids:
            notes.append(
                f"PO {po.po_id} cites unknown supplier '{po.supplier_id}'; "
                "excluded from coverage"
            )
            continue
        pos_by_material.setdefault(po.material, []).append(po)

    gaps: list[CoverageGap] = []
    checked_materials = sorted(events_by_material)
    for material in checked_materials:
        events = events_by_material[material]
        inv = inventory_by_material.get(material)
        net_inventory = (inv.on_hand_units - inv.safety_stock_units) if inv else 0.0
        pos = pos_by_material.get(material, [])

        cumulative_demand = 0.0
        for needed_in_hours, required_units, _order_id in events:
            cumulative_demand += required_units
            arrived = [po for po in pos if po.promised_in_hours <= needed_in_hours]
            cumulative_available = net_inventory + sum(po.quantity_units for po in arrived)
            if cumulative_demand > cumulative_available:
                gaps.append(CoverageGap(
                    gap_id=f"gap::{material}::1",
                    material=material,
                    shortfall_units=round(cumulative_demand - cumulative_available, 3),
                    at_hours=needed_in_hours,
                    single_sourced=supplier_count.get(material, 0) == 1,
                    contributing_po_ids=sorted(po.po_id for po in arrived),
                ))
                break  # one gap per material: the point coverage first breaks

        if material not in {g.material for g in gaps} and supplier_count.get(material, 0) == 1:
            notes.append(f"{material} is single-sourced (only "
                         f"{next(s.supplier_id for s in DEMO_SUPPLIERS if material in s.materials)}) "
                         "but currently fully covered")

    return gaps, checked_materials, notes


def _canonical_assessment_id(gaps: list[CoverageGap]) -> str:
    payload = json.dumps(
        {
            "requirements": [r.model_dump(mode="json") for r in _aggregate_requirements()],
            "inventory": [p.model_dump(mode="json") for p in DEMO_INVENTORY],
            "purchase_orders": [po.model_dump(mode="json") for po in DEMO_PURCHASE_ORDERS],
            "suppliers": [s.model_dump(mode="json") for s in DEMO_SUPPLIERS],
            "gaps": [g.model_dump(mode="json") for g in gaps],
        },
        sort_keys=True,
    )
    return "cov::" + hashlib.sha256(payload.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def get_material_requirements() -> dict:
    """List material requirements derived from the current production plan
    (BOM x scheduling orders), one row per material: summed units, the
    earliest due date driving it, and the contributing order ids."""
    return {"ok": True, "requirements": [r.model_dump(mode="json") for r in _aggregate_requirements()]}


@mcp.tool()
def get_inventory() -> dict:
    """List on-hand inventory positions with safety stock per material."""
    return {"ok": True, "positions": [p.model_dump(mode="json") for p in DEMO_INVENTORY]}


@mcp.tool()
def get_open_purchase_orders() -> dict:
    """List open/in-transit purchase orders. May reference a supplier not
    present in get_supplier_profiles (a decommissioned or mistyped vendor);
    assess_coverage handles that case explicitly rather than crashing."""
    return {"ok": True, "purchase_orders": [po.model_dump(mode="json") for po in DEMO_PURCHASE_ORDERS]}


@mcp.tool()
def get_supplier_profiles() -> dict:
    """List supplier profiles: materials they can supply, on-time delivery
    rate, and average delay."""
    return {"ok": True, "suppliers": [s.model_dump(mode="json") for s in DEMO_SUPPLIERS]}


@mcp.tool()
def assess_coverage() -> dict:
    """Run the deterministic coverage assessment over current requirements,
    inventory and purchase orders. Returns coverage gaps, the materials
    checked, and an assessment_id that MUST be cited in any supply-chain
    report — gaps not produced by this tool are invalid by policy.

    A purchase order citing an unknown supplier does not crash the
    assessment: it is excluded from coverage math, counted, and surfaced in
    notes.
    """
    gaps, checked_materials, notes = _assess()
    assessment_id = _canonical_assessment_id(gaps)
    return {
        "ok": True,
        "assessment_id": assessment_id,
        "gaps": [g.model_dump(mode="json") for g in gaps],
        "checked_materials": checked_materials,
        "notes": notes,
    }


if __name__ == "__main__":
    mcp.run()
