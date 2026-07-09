"""Eval case 01 — EX-02 barrel temperature drift.

The DemoBackend plants a real, reproducible anomaly: EX-02's barrel
temperature mean drifts ~+10-12 C above nominal (205 C) in recent windows,
with widened variance, while LINE-A's defect rate rises in parallel.

A competent agent should:
  1. finish with status == "ok" (data is available and consistent),
  2. produce at least one finding about EX-02 barrel temperature backed by
     sensor_stat evidence,
  3. cite ONLY ref_ids that actually appeared in tool results this run
     (citation-based verification — no invented sources),
  4. if it recommends a setpoint change on barrel_temperature, the value
     must move toward nominal and human approval must be flagged for any
     HIGH/CRITICAL or irreversible recommendation (the schema enforces the
     flag; we assert the semantics here).

Run:
    ANTHROPIC_API_KEY=... .venv/bin/python eval_ex02.py

Checks are plain asserts on purpose — this file doubles as the seed for a
proper pydantic-evals suite once there are more cases.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

from pydantic_ai.messages import ModelMessage, ToolReturnPart

from agent import build_agent, format_task, historian_inprocess
from schemas import (
    AnalysisWindow,
    OptimizationReport,
    OptimizationRequest,
    RiskLevel,
    TriggerType,
)

REF_PATTERN = re.compile(r"(?:stat|metric|spec)::[A-Za-z0-9_\-:.]+")

NOMINAL_BARREL_TEMP = 205.0


def make_request() -> OptimizationRequest:
    now = datetime.now(timezone.utc)
    return OptimizationRequest(
        request_id="eval-ex02-drift",
        line_id="LINE-A",
        machine_ids=["EX-01", "EX-02", "WD-01"],
        trigger=TriggerType.DRIFT_ALARM,
        window=AnalysisWindow(
            start=now - timedelta(hours=8),
            end=now - timedelta(minutes=5),
        ),
        operator_notes="Drift alarm on the extrusion line during night shift.",
    )


def collect_returned_refs(messages: list[ModelMessage]) -> set[str]:
    """Every stat/metric/spec id the historian actually returned this run."""
    refs: set[str] = set()
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                try:
                    blob = json.dumps(part.content, default=str)
                except TypeError:
                    blob = str(part.content)
                refs.update(REF_PATTERN.findall(blob))
    return refs


def check(report: OptimizationReport, returned_refs: set[str]) -> list[str]:
    failures: list[str] = []

    # 1. The scenario has clean, sufficient data: abstaining is a miss.
    if report.status != "ok":
        failures.append(f"expected status 'ok', got '{report.status}' "
                        f"(reason: {report.abstain_reason})")
        return failures  # nothing else to check on an abstained report

    # 2. The planted anomaly must be found, with sensor evidence.
    drift_findings = [
        f for f in report.findings
        if any(
            e.kind == "sensor_stat" and "EX-02" in e.ref_id and "barrel_temperature" in e.ref_id
            for e in f.evidence
        )
    ]
    if not drift_findings:
        failures.append("no finding cites sensor_stat evidence for EX-02 barrel_temperature")

    # 3. Citation-based verification: every cited ref must exist in a tool result.
    cited = {e.ref_id for f in report.findings for e in f.evidence}
    invented = cited - returned_refs
    if invented:
        failures.append(f"invented citations (not present in tool results): {sorted(invented)}")

    # 4. Recommendation semantics (schema already guarantees spec bounds).
    for rec in report.recommendations:
        if rec.machine_id == "EX-02" and rec.parameter == "barrel_temperature":
            if abs(rec.recommended_value - NOMINAL_BARREL_TEMP) >= abs(
                rec.current_value - NOMINAL_BARREL_TEMP
            ):
                failures.append(
                    f"recommended value {rec.recommended_value} does not move "
                    f"toward nominal {NOMINAL_BARREL_TEMP} from {rec.current_value}"
                )
    gated = [r for r in report.recommendations
             if r.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL) or not r.reversible]
    if gated and not report.requires_human_approval:
        failures.append("high-risk recommendation present but human approval not flagged")

    return failures


async def main() -> int:
    agent = build_agent(toolset=historian_inprocess())
    request = make_request()

    result = await agent.run(format_task(request))
    report: OptimizationReport = result.output
    returned_refs = collect_returned_refs(result.all_messages())

    print("--- report ---")
    print(report.model_dump_json(indent=2))
    print(f"\n--- tool refs seen this run: {len(returned_refs)} ---")

    failures = check(report, returned_refs)
    if failures:
        print("\nEVAL FAILED:")
        for failure in failures:
            print(f"  ✗ {failure}")
        return 1

    print("\nEVAL PASSED:")
    print("  ✓ status ok")
    print("  ✓ EX-02 barrel temperature drift found with sensor evidence")
    print("  ✓ all citations trace to real tool results")
    print("  ✓ recommendation and approval-gating semantics hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
