"""Typed contracts for the Production Scheduling Agent.

Same philosophy as ``schemas.py`` (structural guardrails, fail closed),
different domain invariant: a schedule is a COMPUTED artifact. The LLM
never invents assignments — they must come from a deterministic solver
run, identified by ``solution_id``, and the verifier re-runs the solver
to prove it. Validators here enforce the physics a schedule cannot
violate regardless of who produced it:

- no two assignments overlap on the same line,
- time runs forward and units are positive,
- any late assignment forces human approval (a supervisor accepts
  lateness; an agent does not),
- abstentions are clean (no assignments, stated reason).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from schemas import RiskLevel  # shared severity vocabulary

SCHEDULING_SCHEMA_VERSION = "0.1.0"


class OrderStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"  # pinned to its line; cannot be moved by a plan


class ProductionOrder(BaseModel):
    order_id: str
    product: str
    quantity_units: float = Field(gt=0)
    due_in_hours: float = Field(ge=0, description="due date relative to horizon start")
    status: OrderStatus = OrderStatus.OPEN
    pinned_line: str | None = None  # required when in_progress

    @model_validator(mode="after")
    def _pinning(self) -> "ProductionOrder":
        if self.status == OrderStatus.IN_PROGRESS and not self.pinned_line:
            raise ValueError("in_progress orders must declare pinned_line")
        return self


class LineCapacity(BaseModel):
    line_id: str
    horizon_hours: float = Field(gt=0)
    # product -> units per hour this line can run (absence = not eligible)
    rates_units_per_hour: dict[str, float]


class ScheduleAssignment(BaseModel):
    order_id: str
    line_id: str
    product: str
    units: float = Field(gt=0)
    start_hour: float = Field(ge=0)
    end_hour: float
    due_hour: float = Field(ge=0)
    lateness_hours: float = Field(ge=0)

    @model_validator(mode="after")
    def _time_arrow(self) -> "ScheduleAssignment":
        if self.end_hour <= self.start_hour:
            raise ValueError("end_hour must be after start_hour")
        expected_late = max(0.0, self.end_hour - self.due_hour)
        if abs(expected_late - self.lateness_hours) > 1e-3:
            raise ValueError(
                f"lateness_hours {self.lateness_hours} inconsistent with "
                f"end/due ({expected_late:.3f})"
            )
        return self


class UnscheduledOrder(BaseModel):
    order_id: str
    reason: str = Field(min_length=5, max_length=300)


class ScheduleMetrics(BaseModel):
    total_orders: int = Field(ge=0)
    scheduled: int = Field(ge=0)
    unscheduled: int = Field(ge=0)
    late_orders: int = Field(ge=0)
    max_lateness_hours: float = Field(ge=0)
    utilization: dict[str, float]  # line_id -> fraction of horizon used


class SchedulingRequest(BaseModel):
    request_id: str = Field(min_length=1)
    horizon_hours: float = Field(gt=0, le=24 * 14)
    strategy: Literal["edd"] = "edd"  # extend as new solver strategies land
    notes: str | None = Field(default=None, max_length=1000)


class ScheduleRisk(BaseModel):
    description: str = Field(min_length=10, max_length=300)
    severity: RiskLevel


class ScheduleReport(BaseModel):
    """The scheduling agent's only valid output shape."""

    schema_version: str = SCHEDULING_SCHEMA_VERSION
    request_id: str
    status: Literal["ok", "abstained"]
    abstain_reason: str | None = Field(default=None, max_length=500)
    strategy_used: Literal["edd"] | None = None
    solution_id: str | None = Field(
        default=None, description="solver run this schedule came from"
    )
    assignments: list[ScheduleAssignment] = Field(default_factory=list)
    unscheduled: list[UnscheduledOrder] = Field(default_factory=list)
    metrics: ScheduleMetrics | None = None
    risks: list[ScheduleRisk] = Field(default_factory=list)
    requires_human_approval: bool = False
    summary: str = Field(max_length=800)

    @model_validator(mode="after")
    def _gates(self) -> "ScheduleReport":
        if self.status == "abstained":
            if self.assignments or self.solution_id or self.metrics:
                raise ValueError("abstained schedule must carry no plan artifacts")
            if not self.abstain_reason:
                raise ValueError("abstained schedule must state a reason")
            return self

        if self.assignments and not self.solution_id:
            raise ValueError("a schedule with assignments must cite its solution_id")
        if self.assignments and not self.strategy_used:
            raise ValueError("a schedule with assignments must declare strategy_used")

        # Physics: no overlaps per line.
        by_line: dict[str, list[ScheduleAssignment]] = {}
        for a in self.assignments:
            by_line.setdefault(a.line_id, []).append(a)
        for line_id, items in by_line.items():
            items.sort(key=lambda a: a.start_hour)
            for prev, nxt in zip(items, items[1:]):
                if nxt.start_hour < prev.end_hour - 1e-6:
                    raise ValueError(
                        f"overlap on {line_id}: {prev.order_id} ends {prev.end_hour} "
                        f"but {nxt.order_id} starts {nxt.start_hour}"
                    )

        # Lateness is a human decision, not an agent's.
        if any(a.lateness_hours > 0 for a in self.assignments):
            object.__setattr__(self, "requires_human_approval", True)
        return self
