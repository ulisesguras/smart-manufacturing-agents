"""Orchestrator — closes the creator/verifier loop.

Flow
----
    request ──> creator ──> report ──> verifier ──> verdict
                   ▲                                   │
                   └── revision task (issues only) ◄───┘  (max N revisions)

Principles wired in:

- **Isolation**: the verifier never fixes anything. Its issues travel back
  to the creator, who revises its own work. Critic and creator share no
  reasoning, only the report and the issue list.
- **Clean context**: every revision is a FRESH run — original request +
  previous report + verifier issues. No accumulated conversation history
  poisoning later attempts.
- **Kill switch**: at most ``max_revisions`` revision rounds. If the loop
  does not converge, the outcome is REJECTED — fail closed. Nothing
  unverified ever leaves the orchestrator.
- **Audit trail**: every attempt (report + verdict) is recorded in the
  outcome. This is the raw material for episodic memory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic_ai import Agent

from agent import build_agent, format_task
from schemas import (
    AttemptRecord,
    Disposition,
    OptimizationReport,
    OptimizationRequest,
    OrchestrationOutcome,
    VerifierIssue,
    VerifierVerdict,
)
from verifier import build_verifier, run_verification

if TYPE_CHECKING:
    from episodic_memory import PlantEpisodicMemory

logger = logging.getLogger("orchestrator")

DEFAULT_MAX_REVISIONS = 2


def format_revision_task(
    request: OptimizationRequest,
    previous_report: OptimizationReport,
    issues: list[VerifierIssue],
) -> str:
    """Fresh-context revision prompt: request + your report + the audit.

    Deliberately NOT a conversation continuation — the creator re-derives
    from clean inputs instead of defending its earlier reasoning.
    """
    issue_lines = "\n".join(
        f"- [{i.severity}] {i.description}"
        + (f" (finding: {i.related_finding_id})" if i.related_finding_id else "")
        for i in issues
    )
    return (
        "REVISION TASK. Your previous report on this request was audited and "
        "rejected by an independent verifier. Address every blocker; treat "
        "warnings as strong suggestions. Re-retrieve data as needed — do not "
        "assume your previous tool results are still trustworthy.\n\n"
        "Original request:\n"
        f"```json\n{request.model_dump_json(indent=2)}\n```\n\n"
        "Your previous report (REJECTED):\n"
        f"```json\n{previous_report.model_dump_json(indent=2)}\n```\n\n"
        "Verifier issues to address:\n"
        f"{issue_lines}\n"
    )


def _disposition_for(report: OptimizationReport, verdict: VerifierVerdict) -> Disposition:
    if not verdict.approved:
        return Disposition.REJECTED
    if report.status == "abstained":
        return Disposition.ABSTAINED
    if report.requires_human_approval:
        return Disposition.PENDING_HUMAN_APPROVAL
    return Disposition.APPROVED


async def orchestrate(
    request: OptimizationRequest,
    *,
    creator: Agent[None, OptimizationReport] | None = None,
    verifier: Agent[None, VerifierVerdict] | None = None,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
    memory: "PlantEpisodicMemory | None" = None,
) -> OrchestrationOutcome:
    creator = creator or build_agent()
    verifier = verifier or build_verifier()

    def finalize(outcome: OrchestrationOutcome) -> OrchestrationOutcome:
        """Single exit point: gated episodic write happens here, in code.

        The gate itself lives in PlantEpisodicMemory.record_outcome —
        REJECTED / VERIFICATION_FAILED never enter memory.
        """
        if memory is not None:
            episode = memory.record_outcome(request, outcome)
            if episode is not None:
                logger.info("episode %s recorded for %s",
                            episode.episode_id, request.request_id)
            else:
                logger.info("memory gate refused %s (%s)",
                            request.request_id, outcome.disposition.value)
        return outcome

    attempts: list[AttemptRecord] = []
    task = format_task(request)

    for attempt_number in range(1, max_revisions + 2):  # initial + revisions
        logger.info("attempt %d/%d for %s", attempt_number, max_revisions + 1,
                    request.request_id)

        result = await creator.run(task)
        report: OptimizationReport = result.output

        try:
            verdict = await run_verification(verifier, request, report)
        except Exception:
            # Fail closed: an unverified report never leaves the orchestrator.
            logger.exception("verification errored for %s", request.request_id)
            attempts.append(AttemptRecord(attempt=attempt_number, report=report))
            return finalize(OrchestrationOutcome(
                request_id=request.request_id,
                disposition=Disposition.VERIFICATION_FAILED,
                revisions_used=attempt_number - 1,
                attempts=attempts,
            ))

        attempts.append(
            AttemptRecord(attempt=attempt_number, report=report, verdict=verdict)
        )

        if verdict.approved:
            outcome = OrchestrationOutcome(
                request_id=request.request_id,
                disposition=_disposition_for(report, verdict),
                revisions_used=attempt_number - 1,
                attempts=attempts,
            )
            logger.info("%s resolved: %s after %d revision(s)",
                        request.request_id, outcome.disposition.value,
                        outcome.revisions_used)
            return finalize(outcome)

        blockers = [i for i in verdict.issues if i.severity == "blocker"]
        logger.info("attempt %d rejected with %d blocker(s)",
                    attempt_number, len(blockers))
        # Clean-context revision for the next round.
        task = format_revision_task(request, report, verdict.issues)

    outcome = OrchestrationOutcome(
        request_id=request.request_id,
        disposition=Disposition.REJECTED,
        revisions_used=max_revisions,
        attempts=attempts,
    )
    logger.warning("%s REJECTED after %d attempts — fail closed",
                   request.request_id, len(attempts))
    return finalize(outcome)


if __name__ == "__main__":
    # Full pipeline against the demo drift. Requires ANTHROPIC_API_KEY.
    import asyncio

    from eval_ex02 import make_request

    async def main() -> None:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
        outcome = await orchestrate(make_request())
        print(outcome.model_dump_json(indent=2))

    asyncio.run(main())
