"""Typed contracts for the Supply-Chain Resilience Agent.

Same philosophy as ``schemas.py`` / ``scheduling_schemas.py`` (structural
guardrails, fail closed), different domain invariant: a coverage gap is a
COMPUTED artifact. The LLM never computes a shortfall — every ``CoverageGap``
must come verbatim from a deterministic ``assess_coverage`` tool run,
identified by ``assessment_id``, and the verifier re-runs the tool to prove
it. Validators here enforce what a supply picture cannot violate regardless
of who produced it:

- a "gap" with zero or negative shortfall is not a gap and must not exist,
- a HIGH/CRITICAL risk needs primary evidence (a gap or a supplier), not
  just narrative,
- any mitigation that costs money forces human approval — the schema makes
  this impossible to opt out of,
- abstentions are clean (no gaps, no risks, no assessment_id, stated
  reason).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from schemas import RiskLevel  # shared severity vocabulary

SUPPLY_SCHEMA_VERSION = "0.1.0"

MaterialId = str  # e.g. "M-PEL"


class SupplierProfile(BaseModel):
    supplier_id: str
    name: str
    materials: list[MaterialId] = Field(min_length=1)
    otd_rate: float = Field(ge=0.0, le=1.0, description="on-time delivery history")
    avg_delay_hours: float = Field(ge=0.0)


class PurchaseOrder(BaseModel):
    po_id: str
    material: MaterialId
    quantity_units: float = Field(gt=0)
    supplier_id: str
    promised_in_hours: float = Field(ge=0, description="relative to horizon start")
    status: Literal["open", "in_transit"]


class InventoryPosition(BaseModel):
    material: MaterialId
    on_hand_units: float = Field(ge=0)
    safety_stock_units: float = Field(ge=0)


class MaterialRequirement(BaseModel):
    material: MaterialId
    required_units: float = Field(gt=0)
    needed_in_hours: float = Field(ge=0)
    source_order_ids: list[str] = Field(min_length=1, description="production orders driving it")


class CoverageGap(BaseModel):
    gap_id: str = Field(min_length=1, description="citable evidence handle, e.g. gap::M-RES::1")
    material: MaterialId
    shortfall_units: float = Field(gt=0)
    at_hours: float = Field(ge=0, description="when coverage breaks")
    single_sourced: bool
    contributing_po_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _real_gap(self) -> "CoverageGap":
        # Redundant with Field(gt=0) but stated explicitly: a non-gap must
        # never be emitted as a CoverageGap.
        if self.shortfall_units <= 0:
            raise ValueError("shortfall_units must be > 0 — a non-gap must not be emitted")
        return self


class Mitigation(str, Enum):
    MONITOR = "monitor"                        # only action an agent may own
    EXPEDITE_PO = "expedite_po"                 # costs money -> human gate
    REORDER = "reorder"                         # costs money -> human gate
    ALTERNATE_SUPPLIER = "alternate_supplier"    # costs money -> human gate


_MONEY_MITIGATIONS = {Mitigation.EXPEDITE_PO, Mitigation.REORDER, Mitigation.ALTERNATE_SUPPLIER}


class SupplyRisk(BaseModel):
    risk_id: str = Field(min_length=1)
    description: str = Field(min_length=10, max_length=300)
    severity: RiskLevel
    evidence_refs: list[str] = Field(min_length=1, description="gap::/po::/supplier::/inv:: ids")
    mitigation: Mitigation

    @model_validator(mode="after")
    def _big_claim_needs_primary_evidence(self) -> "SupplyRisk":
        if self.severity in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            has_primary = any(
                ref.startswith("gap::") or ref.startswith("supplier::")
                for ref in self.evidence_refs
            )
            if not has_primary:
                raise ValueError(
                    f"risk {self.risk_id}: severity {self.severity.value} requires at "
                    "least one gap:: or supplier:: evidence reference"
                )
        return self


class SupplyChainReport(BaseModel):
    """The supply-chain agent's only valid output shape."""

    schema_version: str = SUPPLY_SCHEMA_VERSION
    request_id: str
    status: Literal["ok", "abstained"]
    abstain_reason: str | None = Field(default=None, max_length=500)
    assessment_id: str | None = Field(
        default=None, description="the assess_coverage run this report cites"
    )
    gaps: list[CoverageGap] = Field(default_factory=list, description="copied verbatim from the tool")
    risks: list[SupplyRisk] = Field(default_factory=list, description="the LLM's interpretation layer")
    requires_human_approval: bool = False
    summary: str = Field(max_length=800)

    @model_validator(mode="after")
    def _gates(self) -> "SupplyChainReport":
        if self.status == "abstained":
            if self.gaps or self.risks or self.assessment_id:
                raise ValueError("abstained report must carry no gaps, risks, or assessment_id")
            if not self.abstain_reason:
                raise ValueError("abstained report must state a reason")
            return self

        if (self.gaps or self.risks) and not self.assessment_id:
            raise ValueError("a report with gaps or risks must cite its assessment_id")

        # Every gap:: evidence_ref must resolve against gaps actually on this report.
        known_gap_ids = {g.gap_id for g in self.gaps}
        for risk in self.risks:
            cited_gaps = {ref for ref in risk.evidence_refs if ref.startswith("gap::")}
            missing = cited_gaps - known_gap_ids
            if missing:
                raise ValueError(
                    f"risk {risk.risk_id} cites unknown gap evidence: {sorted(missing)}"
                )

        # Computed risk gate: any money-costing mitigation forces the human gate.
        needs_gate = any(risk.mitigation in _MONEY_MITIGATIONS for risk in self.risks)
        if needs_gate:
            object.__setattr__(self, "requires_human_approval", True)
        return self
