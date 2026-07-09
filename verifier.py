"""Verifier Agent — deterministic prechecks + LLM review.

Two layers, in order:

1. **Deterministic prechecks (code, no LLM).** Anything that can be
   verified mechanically is verified mechanically:
   - every cited ``ref_id`` is re-fetched from the historian and must be
     reproducible (the citation encodes machine/parameter/window, so the
     exact query can be replayed);
   - the spec envelope claimed inside each recommendation is compared
     against the historian's real spec — this closes the hole where a
     creator "lies" about the envelope to sneak an out-of-spec value past
     schema validation;
   - the recommended value is re-checked against the REAL envelope.

2. **LLM review (skeptical reviewer).** Judgment calls only: does the
   evidence support the claim, is confidence calibrated, is risk classified
   sensibly, was abstention appropriate. The LLM gets the same read-only
   historian toolset to spot-check numbers independently.

``run_verification`` merges both: deterministic blockers force
``approved=False`` regardless of what the LLM concluded. Code outranks
model on facts; model outranks code on judgment.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset

from agent import historian_inprocess
from schemas import (
    OptimizationReport,
    OptimizationRequest,
    VerifierIssue,
    VerifierVerdict,
)

_HERE = Path(__file__).parent

DEFAULT_MODEL = os.environ.get(
    "VERIFIER_MODEL", os.environ.get("PROCESS_OPT_MODEL", "anthropic:claude-sonnet-4-6")
)


def load_verifier_prompt() -> str:
    return (_HERE / "verifier_prompt.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Layer 1 — deterministic prechecks
# --------------------------------------------------------------------------

def _parse_wid(wid: str) -> tuple[str, str] | None:
    """'20260706T0200-20260706T1000' -> ISO start/end, or None."""
    try:
        start_raw, end_raw = wid.split("-")
        start = datetime.strptime(start_raw, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
        end = datetime.strptime(end_raw, "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return start.isoformat(), end.isoformat()


def _blocker(description: str, finding_id: str | None = None) -> VerifierIssue:
    return VerifierIssue(severity="blocker", description=description,
                         related_finding_id=finding_id)


def deterministic_prechecks(report: OptimizationReport) -> list[VerifierIssue]:
    """Replay every citation and spec claim against the historian.

    Imports the historian module lazily so it runs against whichever
    backend the environment selected (demo or csv) — the same source the
    creator agent used.

    Fidelity note: replay is only as strong as the backend. The demo
    backend synthesizes data for ANY window, so a well-formed citation
    with a fabricated window "reproduces" there; against real, finite
    data (CSV or a production historian) the same citation fails. Run
    verification evals against a real-data backend (see eval_verifier
    case B2).
    """
    import historian_mcp_server as historian

    issues: list[VerifierIssue] = []
    if report.status == "abstained":
        return issues  # nothing mechanical to replay; the LLM judges intent

    # -- 1. Citations must be reproducible -----------------------------------
    for finding in report.findings:
        for ev in finding.evidence:
            parts = ev.ref_id.split("::")
            reproduced = False
            if ev.kind == "sensor_stat" and len(parts) == 4:
                _, machine_id, parameter, wid = parts
                window = _parse_wid(wid)
                if window:
                    result = historian.get_sensor_stats(
                        machine_id, window[0], window[1], parameter
                    )
                    reproduced = result.get("ok", False) and any(
                        s["stat_id"] == ev.ref_id for s in result.get("stats", [])
                    )
            elif ev.kind == "quality_metric" and len(parts) == 4:
                _, line_id, _metric, wid = parts
                window = _parse_wid(wid)
                if window:
                    result = historian.get_quality_metrics(line_id, window[0], window[1])
                    reproduced = result.get("ok", False) and any(
                        m["metric_id"] == ev.ref_id for m in result.get("metrics", [])
                    )
            elif ev.kind == "machine_spec" and len(parts) == 3:
                _, machine_id, _parameter = parts
                result = historian.get_machine_spec(machine_id)
                reproduced = result.get("ok", False) and any(
                    s["spec_id"] == ev.ref_id for s in result.get("specs", [])
                )
            if not reproduced:
                issues.append(_blocker(
                    f"citation '{ev.ref_id}' could not be reproduced from the historian",
                    finding.finding_id,
                ))

    # -- 2. Spec envelopes must match the source of truth --------------------
    for i, rec in enumerate(report.recommendations):
        result = historian.get_machine_spec(rec.machine_id)
        real = None
        if result.get("ok"):
            real = next(
                (s for s in result["specs"] if s["parameter"] == rec.parameter), None
            )
        if real is None:
            issues.append(_blocker(
                f"recommendation #{i}: no real spec found for "
                f"{rec.machine_id}/{rec.parameter}"
            ))
            continue
        if not (
            math.isclose(rec.spec_min, real["min_allowed"], rel_tol=1e-6)
            and math.isclose(rec.spec_max, real["max_allowed"], rel_tol=1e-6)
        ):
            issues.append(_blocker(
                f"recommendation #{i}: claimed spec envelope "
                f"[{rec.spec_min}, {rec.spec_max}] does not match the real spec "
                f"[{real['min_allowed']}, {real['max_allowed']}] for "
                f"{rec.machine_id}/{rec.parameter}"
            ))
        if not (real["min_allowed"] <= rec.recommended_value <= real["max_allowed"]):
            issues.append(_blocker(
                f"recommendation #{i}: recommended value {rec.recommended_value} "
                f"{rec.unit} is outside the REAL spec envelope "
                f"[{real['min_allowed']}, {real['max_allowed']}]"
            ))

    return issues


# --------------------------------------------------------------------------
# Layer 2 — LLM reviewer
# --------------------------------------------------------------------------

def build_verifier(
    toolset: MCPToolset | None = None,
    model: str = DEFAULT_MODEL,
) -> Agent[None, VerifierVerdict]:
    return Agent(
        model,
        name="verifier",
        instructions=load_verifier_prompt(),
        output_type=VerifierVerdict,
        toolsets=[toolset or historian_inprocess()],
        retries=2,
        defer_model_check=True,
    )


def format_review_task(
    request: OptimizationRequest,
    report: OptimizationReport,
    precheck_issues: list[VerifierIssue],
) -> str:
    precheck_block = (
        "\n".join(f"- [{i.severity}] {i.description}" for i in precheck_issues)
        or "- none"
    )
    return (
        "Audit the following optimization report.\n\n"
        "Original request:\n"
        f"```json\n{request.model_dump_json(indent=2)}\n```\n\n"
        "Report under review:\n"
        f"```json\n{report.model_dump_json(indent=2)}\n```\n\n"
        "Deterministic precheck results (already confirmed by code — do not "
        "re-litigate them, focus your spot-checks on judgment):\n"
        f"{precheck_block}\n"
    )


# --------------------------------------------------------------------------
# Pipeline — merge code and model
# --------------------------------------------------------------------------

def merge_verdicts(
    llm: VerifierVerdict, precheck_issues: list[VerifierIssue]
) -> VerifierVerdict:
    """Deterministic blockers override the LLM. Facts outrank judgment."""
    has_code_blockers = any(i.severity == "blocker" for i in precheck_issues)
    approved = llm.approved and not has_code_blockers
    confidence = llm.confidence_score
    if has_code_blockers:
        # Rejection driven by reproducible facts is high-confidence by nature,
        # but must not satisfy the approval bar semantics; keep the LLM's
        # score unless it would misleadingly read as an endorsement.
        confidence = max(confidence, 9)
    return VerifierVerdict(
        request_id=llm.request_id,
        approved=approved,
        confidence_score=confidence,
        issues=[*precheck_issues, *llm.issues],
    )


async def run_verification(
    verifier: Agent[None, VerifierVerdict],
    request: OptimizationRequest,
    report: OptimizationReport,
) -> VerifierVerdict:
    precheck_issues = deterministic_prechecks(report)
    result = await verifier.run(format_review_task(request, report, precheck_issues))
    return merge_verdicts(result.output, precheck_issues)


if __name__ == "__main__":
    # Full parent-child smoke: creator produces, verifier audits.
    # Requires ANTHROPIC_API_KEY.
    import asyncio

    from agent import build_agent, run_optimization
    from eval_ex02 import make_request

    async def main() -> None:
        request = make_request()
        report = await run_optimization(build_agent(), request)
        verdict = await run_verification(build_verifier(), request, report)
        print(report.model_dump_json(indent=2))
        print(verdict.model_dump_json(indent=2))

    asyncio.run(main())
