"""Typed contracts for the Process Optimization Agent.

Design principles
-----------------
1. Every boundary the agent crosses (request, tool result, report) is a
   validated Pydantic model. Output that does not parse against
   ``OptimizationReport`` is a *structural hallucination* and gets rejected
   before it reaches the Verifier or a human.
2. Citation-based verification: no source, no output. Every ``Finding``
   must reference at least one concrete tool result (``EvidenceRef``).
3. Hard-coded sanity bounds live in validators, never in prompts.
   A recommended setpoint outside machine spec fails closed here,
   regardless of what the LLM produced.
4. Risk gating is computed, not asserted: the model cannot claim an
   irreversible action is low-risk, and any HIGH/CRITICAL recommendation
   forces ``requires_human_approval = True``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "0.1.0"


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """Risk of applying a recommendation to the physical plant."""

    LOW = "low"            # informational only, no actuation implied
    MEDIUM = "medium"      # reversible change, well inside spec bounds
    HIGH = "high"          # near spec limits or directly affects quality
    CRITICAL = "critical"  # irreversible or safety-relevant


class TriggerType(str, Enum):
    DRIFT_ALARM = "drift_alarm"
    QUALITY_DEVIATION = "quality_deviation"
    SCHEDULED_REVIEW = "scheduled_review"
    MANUAL_REQUEST = "manual_request"


# --------------------------------------------------------------------------
# Request (orchestrator -> agent)
# --------------------------------------------------------------------------

class AnalysisWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _validate_window(self) -> "AnalysisWindow":
        if self.end <= self.start:
            raise ValueError("window end must be after window start")
        if self.end > datetime.now(timezone.utc):
            raise ValueError("analysis window cannot extend into the future")
        return self


class OptimizationRequest(BaseModel):
    """Task handed to the agent by the orchestrator."""

    request_id: str = Field(min_length=1)
    line_id: str = Field(min_length=1)
    machine_ids: list[str] = Field(min_length=1, max_length=10)
    trigger: TriggerType
    window: AnalysisWindow
    operator_notes: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------
# Tool-facing models (what the historian MCP server returns)
# --------------------------------------------------------------------------

class SensorStat(BaseModel):
    """Aggregated statistics for one sensor over a window.

    ``stat_id`` is the citation handle: findings reference it as evidence.
    """

    stat_id: str
    machine_id: str
    parameter: str            # e.g. "barrel_temperature"
    unit: str                 # e.g. "celsius"
    mean: float
    std: float
    minimum: float
    maximum: float
    sample_count: int = Field(ge=1)
    window: AnalysisWindow


class MachineSpec(BaseModel):
    """Allowed operating envelope for one parameter of one machine.

    Source of truth for sanity bounds. Comes from the spec sheet,
    never from the LLM.
    """

    spec_id: str
    machine_id: str
    parameter: str
    unit: str
    min_allowed: float
    max_allowed: float
    nominal: float

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MachineSpec":
        if not (self.min_allowed <= self.nominal <= self.max_allowed):
            raise ValueError("nominal must lie within [min_allowed, max_allowed]")
        return self


class QualityMetric(BaseModel):
    """One quality KPI for a line over a window (e.g. defect rate)."""

    metric_id: str
    line_id: str
    metric: str               # e.g. "defect_rate"
    value: float
    unit: str                 # e.g. "percent"
    window: AnalysisWindow


# --------------------------------------------------------------------------
# Evidence and findings (agent output building blocks)
# --------------------------------------------------------------------------

class EvidenceRef(BaseModel):
    """Citation to a concrete tool result. No source -> no output."""

    kind: Literal["sensor_stat", "quality_metric", "machine_spec"]
    ref_id: str = Field(min_length=1, description="stat_id / metric_id / spec_id")
    note: str = Field(max_length=200, description="what this reference supports")


class Finding(BaseModel):
    """One factual claim about the process, backed by evidence."""

    finding_id: str = Field(min_length=1)
    statement: str = Field(min_length=10, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ParameterRecommendation(BaseModel):
    """A proposed setpoint change. Never executed directly by this agent."""

    machine_id: str
    parameter: str
    unit: str
    current_value: float
    recommended_value: float
    spec_min: float
    spec_max: float
    rationale_finding_ids: list[str] = Field(min_length=1)
    expected_effect: str = Field(max_length=300)
    risk: RiskLevel
    reversible: bool

    @model_validator(mode="after")
    def _sanity_bounds(self) -> "ParameterRecommendation":
        # Hard-coded sanity check: fail closed, no exceptions.
        if not (self.spec_min <= self.recommended_value <= self.spec_max):
            raise ValueError(
                f"recommended_value {self.recommended_value} {self.unit} outside "
                f"spec envelope [{self.spec_min}, {self.spec_max}] for "
                f"{self.machine_id}/{self.parameter}"
            )
        # The model cannot downplay irreversible actions.
        if not self.reversible and self.risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
            raise ValueError(
                "irreversible recommendations must be rated HIGH or CRITICAL"
            )
        return self


# --------------------------------------------------------------------------
# Final report (agent -> verifier -> human/orchestrator)
# --------------------------------------------------------------------------

class OptimizationReport(BaseModel):
    """The agent's only valid output shape."""

    schema_version: str = SCHEMA_VERSION
    request_id: str
    status: Literal["ok", "abstained"]
    abstain_reason: str | None = Field(default=None, max_length=500)
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[ParameterRecommendation] = Field(default_factory=list)
    requires_human_approval: bool = False
    summary: str = Field(max_length=800)

    @model_validator(mode="after")
    def _gates(self) -> "OptimizationReport":
        # Abstention is a first-class outcome, not an error — but it must
        # be clean: no findings, no recommendations, and a stated reason.
        if self.status == "abstained":
            if self.findings or self.recommendations:
                raise ValueError("abstained reports must carry no findings or recommendations")
            if not self.abstain_reason:
                raise ValueError("abstained reports must state a reason")
            return self

        # Every recommendation must trace back to declared findings.
        known = {f.finding_id for f in self.findings}
        for rec in self.recommendations:
            missing = set(rec.rationale_finding_ids) - known
            if missing:
                raise ValueError(f"recommendation cites unknown findings: {missing}")

        # Computed risk gate: the model cannot opt out of human approval.
        needs_gate = any(
            rec.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL) or not rec.reversible
            for rec in self.recommendations
        )
        if needs_gate:
            object.__setattr__(self, "requires_human_approval", True)
        return self


# --------------------------------------------------------------------------
# Verifier contract (parent-child topology)
# --------------------------------------------------------------------------

class VerifierIssue(BaseModel):
    severity: Literal["blocker", "warning"]
    description: str = Field(max_length=400)
    related_finding_id: str | None = None


class VerifierVerdict(BaseModel):
    """Output of the critic agent. It sees only the report, never the
    creator's reasoning, and it sends feedback rather than fixing things."""

    request_id: str
    approved: bool
    confidence_score: int = Field(ge=1, le=10)
    issues: list[VerifierIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistency(self) -> "VerifierVerdict":
        has_blockers = any(i.severity == "blocker" for i in self.issues)
        if self.approved and has_blockers:
            raise ValueError("a verdict with blocker issues cannot be approved")
        if self.approved and self.confidence_score < 8:
            raise ValueError("approval requires confidence_score >= 8")
        return self


# --------------------------------------------------------------------------
# Orchestration contracts (creator -> verifier -> revision loop)
# --------------------------------------------------------------------------

class Disposition(str, Enum):
    """Terminal state of an orchestrated request."""

    APPROVED = "approved"                          # verified, no human gate needed
    PENDING_HUMAN_APPROVAL = "pending_human_approval"  # verified, awaiting signature
    ABSTAINED = "abstained"                        # verified clean abstention
    REJECTED = "rejected"                          # revisions exhausted, fail closed
    VERIFICATION_FAILED = "verification_failed"    # verifier errored, fail closed


class AttemptRecord(BaseModel):
    """One creator/verifier round. The audit trail: every attempt is kept,
    which later feeds episodic memory ('writes down everything')."""

    attempt: int = Field(ge=1)
    report: OptimizationReport
    verdict: VerifierVerdict | None = None  # None only if verification errored


class OrchestrationOutcome(BaseModel):
    """Final, auditable result of one orchestrated optimization request."""

    request_id: str
    disposition: Disposition
    revisions_used: int = Field(ge=0)
    attempts: list[AttemptRecord] = Field(min_length=1)

    @property
    def final_report(self) -> OptimizationReport:
        return self.attempts[-1].report

    @property
    def final_verdict(self) -> VerifierVerdict | None:
        return self.attempts[-1].verdict

    @model_validator(mode="after")
    def _coherence(self) -> "OrchestrationOutcome":
        last = self.attempts[-1]
        verdict, report = last.verdict, last.report

        if self.disposition == Disposition.VERIFICATION_FAILED:
            if verdict is not None:
                raise ValueError("verification_failed requires a missing final verdict")
            return self
        if verdict is None:
            raise ValueError("every non-error disposition requires a final verdict")

        if self.disposition == Disposition.REJECTED:
            if verdict.approved:
                raise ValueError("rejected outcome cannot carry an approved verdict")
            return self

        # All remaining dispositions require verifier approval.
        if not verdict.approved:
            raise ValueError(f"{self.disposition} requires an approved final verdict")
        if self.disposition == Disposition.ABSTAINED and report.status != "abstained":
            raise ValueError("abstained disposition requires an abstained report")
        if self.disposition == Disposition.APPROVED and (
            report.status != "ok" or report.requires_human_approval
        ):
            raise ValueError("approved disposition requires ok status without human gate")
        if self.disposition == Disposition.PENDING_HUMAN_APPROVAL and not (
            report.status == "ok" and report.requires_human_approval
        ):
            raise ValueError("pending_human_approval requires ok status with human gate")
        return self
