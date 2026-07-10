# SPEC — Supply-Chain Resilience Agent (third specialist)

> Implementation brief for Claude Code. This spec defines WHAT done means;
> the repo's CLAUDE.md defines HOW to build it. Follow the established
> skeleton: `*_schemas.py` -> `*_prompt.md` -> MCP server -> agent factory
> -> deterministic prechecks -> eval file with positive AND negative cases.

## Mission

Answer one question for the plant: **does the production plan have the
materials to actually happen?** The agent detects coverage gaps (on-hand
inventory + inbound purchase orders insufficient for scheduled demand),
single-source dependencies, and supplier reliability risks — then reports
them with evidence and proposes mitigations. It never purchases, never
expedites, never contacts suppliers: any mitigation that costs money is a
recommendation behind the human gate.

Core invariant, same family as the other two specialists: **the LLM never
computes a shortfall.** Coverage math is deterministic and lives inside an
MCP tool that returns gaps tagged with a reproducible `assessment_id`.
The agent interprets; the verifier re-runs and compares.

## New files

- `supply_schemas.py`
- `supply_prompt.md`
- `supply_mcp_server.py`
- `supply_agent.py`   (factory + deterministic prechecks, mirroring
  `scheduling_agent.py`)
- `eval_supply.py`

Do NOT modify: `schemas.py`, `scheduling_schemas.py`, or any existing
validator. Reuse `RiskLevel` and `VerifierIssue` from `schemas.py`.

## Contracts (supply_schemas.py)

All models Pydantic, fail-closed validators, same style as
`scheduling_schemas.py`.

```
MaterialId = str  # e.g. "M-PEL"

class SupplierProfile:
    supplier_id: str
    name: str
    materials: list[MaterialId]          # what they can supply
    otd_rate: float (0..1)               # on-time delivery history
    avg_delay_hours: float (>=0)

class PurchaseOrder:
    po_id: str
    material: MaterialId
    quantity_units: float (>0)
    supplier_id: str
    promised_in_hours: float (>=0)       # relative to horizon start
    status: Literal["open", "in_transit"]

class InventoryPosition:
    material: MaterialId
    on_hand_units: float (>=0)
    safety_stock_units: float (>=0)

class MaterialRequirement:
    material: MaterialId
    required_units: float (>0)
    needed_in_hours: float (>=0)
    source_order_ids: list[str]          # production orders driving it

class CoverageGap:
    gap_id: str                          # citable evidence handle
    material: MaterialId
    shortfall_units: float (>0)
    at_hours: float (>=0)                # when coverage breaks
    single_sourced: bool
    contributing_po_ids: list[str]
    # validator: shortfall_units > 0 (a non-gap must not be emitted)

class Mitigation(str, Enum):
    MONITOR = "monitor"                  # only action an agent may own
    EXPEDITE_PO = "expedite_po"          # costs money -> human gate
    REORDER = "reorder"                  # costs money -> human gate
    ALTERNATE_SUPPLIER = "alternate_supplier"  # costs money -> human gate

class SupplyRisk:
    risk_id: str
    description: str (10..300)
    severity: RiskLevel                  # reuse from schemas.py
    evidence_refs: list[str] (min 1)     # gap::/po::/supplier::/inv:: ids
    mitigation: Mitigation
    # validator: severity HIGH/CRITICAL requires >= 1 gap:: or supplier::
    # reference (a big claim needs primary evidence)

class SupplyChainReport:
    schema_version: "0.1.0"
    request_id: str
    status: Literal["ok", "abstained"]
    abstain_reason: str | None
    assessment_id: str | None            # the tool run this report cites
    gaps: list[CoverageGap]              # copied VERBATIM from the tool
    risks: list[SupplyRisk]              # the LLM's interpretation layer
    requires_human_approval: bool = False
    summary: str (<=800)

    # validators (fail closed):
    # - abstained  -> no gaps, no risks, no assessment_id, reason required
    # - ok with gaps or risks -> assessment_id required
    # - any risk.mitigation != MONITOR  -> force
    #   requires_human_approval = True via object.__setattr__
    # - every evidence_ref with prefix gap:: must exist in report.gaps
```

## MCP server (supply_mcp_server.py)

FastMCP server `plant-supply`. Read-only. Structured errors
(`{ok: false, error: {code, message}}`), never raw tracebacks.

Demo data — deterministic, tied to the scheduling demo world:

- BOM: product A -> 2.0 M-PEL/unit; B -> 1.5 M-PEL + 0.5 M-ADD;
  C -> 1.0 M-RES.
- Demand derives from `scheduling_mcp_server.DEMO_ORDERS` (import it):
  material requirements = BOM x order quantities, needed_in_hours =
  order.due_in_hours. This is the first cross-agent seam — demand comes
  from the same world the scheduler plans.
- Suppliers: SUP-1 (M-PEL + M-RES, otd 0.95), SUP-2 (M-PEL, otd 0.60,
  avg_delay 24h — the unreliable one), SUP-3 (M-ADD only — the single
  source).
- Inventory + open POs sized to PLANT exactly two findings:
  1. an M-RES coverage gap (inventory + inbound < demand from O-1003),
  2. M-ADD fully covered BUT single-sourced through SUP-3.
  Everything else comfortably covered.

Tools:

1. `get_material_requirements()` -> requirements list (each with a
   `req::<material>` id)
2. `get_inventory()` -> positions (`inv::<material>` ids)
3. `get_open_purchase_orders()` -> POs (`po::<id>` ids)
4. `get_supplier_profiles()` -> suppliers (`supplier::<id>` ids)
5. `assess_coverage()` -> the deterministic core:
   - timeline per material: on_hand - safety_stock + POs arriving before
     each need vs cumulative demand
   - emits CoverageGap objects (gap::<material>::<n> ids) and
     single_sourced flags (a material is single-sourced when exactly one
     supplier lists it)
   - returns {ok, assessment_id, gaps, checked_materials, notes}
   - assessment_id = "cov::" + sha256(canonical JSON of inputs+gaps)[:12]
   - a PO citing an unknown supplier must NOT crash the assessment: skip
     it from coverage, count it, and surface it in `notes` (mirror of the
     ghost-pinned-line philosophy)

## Prompt (supply_prompt.md)

Hard rules, ordered by what breaks production first:

1. NEVER compute shortfalls yourself. Every gap in your report comes
   verbatim from `assess_coverage`, and you MUST cite its assessment_id.
2. NEVER invent suppliers, POs, inventory or requirements — every
   evidence_ref must be an id returned by a tool in this session.
3. Any mitigation that costs money (expedite, reorder, alternate
   supplier) is a recommendation, never an action; the schema forces the
   human gate — do not work around it.
4. If tools error or data is missing/contradictory: ABSTAIN with a clear
   reason.
5. JSON only, valid against SupplyChainReport.

Interpretation guidance (where the LLM adds value): prioritize gaps by
severity (how soon, how big, single-sourced?), cross-reference supplier
reliability (a gap fed by SUP-2's 0.60 OTD is worse than the number
suggests), and write the summary for a purchasing manager, leading with
what needs a signature.

## Deterministic prechecks (in supply_agent.py)

`supply_prechecks(report) -> list[VerifierIssue]`, mirroring
`scheduling_prechecks`:

1. Re-run `assess_coverage()`; compare `assessment_id` AND the full gaps
   list verbatim. Mismatch -> blocker ("not a reproducible assessment").
2. Every `evidence_ref` must resolve against freshly fetched tool data
   (po::/supplier::/inv::/req:: ids re-fetched; gap:: ids against the
   re-run). Unresolvable ref -> blocker.
3. Any money-costing mitigation with `requires_human_approval == False`
   -> blocker (defense in depth; the schema should make this impossible).

## Eval (eval_supply.py) — positive AND negative per behavior

1. ASSESSMENT PHYSICS: `assess_coverage` finds exactly the two planted
   findings (M-RES gap with correct shortfall; M-ADD single-sourced with
   NO gap) and is deterministic (same assessment_id twice).
2. SCHEMA GATES (negatives): a report with gaps but no assessment_id
   fails; a zero-shortfall CoverageGap fails; a money-mitigation report
   with requires_human_approval=False comes out forced True; dirty
   abstention (risks attached) fails.
3. HONEST REPORT: built verbatim from a real assess run -> prechecks
   clean.
4. TAMPERED REPORTS (each blocked): (a) hand-edited shortfall number,
   (b) invented gap id cited as evidence, (c) invented supplier id in
   evidence_refs.
5. GHOST SUPPLIER: monkeypatch a PO with supplier_id="SUP-GHOST" into the
   demo data (save/restore in finally, same pattern as eval case 6):
   assess_coverage must not crash, must exclude the PO from coverage, and
   must mention it in notes.
6. AGENT PLUMBING: TestModel end-to-end over the real MCP toolset; human
   gate forced through the live loop when the scripted report proposes
   an expedite.

Print style: match the existing evals (numbered Spanish check lines,
EVAL PASSED/FAILED summary, exit code 0/1).

## Acceptance criteria

- All FIVE offline eval suites pass (the four existing ones untouched and
  green, plus eval_supply.py) with exit code 0.
- No modifications to existing schemas, validators, prompts or evals.
- `PYTHONIOENCODING=utf-8` respected per CLAUDE.md when running on
  Windows consoles.
- CLAUDE.md: add eval_supply.py to the Commands section and the new agent
  to the architecture map — below the hard rules, never touching them.
- Commit message describing the new specialist; push to origin.
