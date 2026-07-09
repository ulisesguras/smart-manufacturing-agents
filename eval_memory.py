"""Eval case 05 — episodic memory (Taxanomy) behaves like plant memory should.

Five checks, all offline:

  1. WRITE GATE      approved / pending / abstained outcomes are recorded;
                     REJECTED and VERIFICATION_FAILED are refused — a bad
                     diagnosis must never poison future recalls.
  2. DURABILITY      a fresh PlantEpisodicMemory instance re-hydrates every
                     episode from the JSONL journal (restart survival).
  3. RECALL RANKING  querying EX-02/barrel_temperature ranks the EX-02
                     episode above an EX-01 episode; unrelated machines
                     return nothing.
  4. LESSONS TRAVEL  verifier blockers earned across attempts are stored in
                     the episode and surface in recall results.
  5. END-TO-END      the orchestrator (scripted agents, real prechecks)
                     writes exactly one episode for an approved run and
                     none for a rejected run.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from schemas import (
    AttemptRecord,
    Disposition,
    OptimizationReport,
    OrchestrationOutcome,
    VerifierVerdict,
)
from episodic_memory import PlantEpisodicMemory
from eval_verifier import fetch_real_refs, make_report, make_request


def approved_verdict(blockers: list[str] | None = None) -> VerifierVerdict:
    return VerifierVerdict(request_id="eval-verifier", approved=True, confidence_score=9)


def rejected_verdict(blockers: list[str]) -> VerifierVerdict:
    return VerifierVerdict(
        request_id="eval-verifier",
        approved=False,
        confidence_score=9,
        issues=[{"severity": "blocker", "description": b} for b in blockers],
    )


def outcome_for(
    report: OptimizationReport,
    disposition: Disposition,
    *,
    prior_blockers: list[str] | None = None,
) -> OrchestrationOutcome:
    attempts = []
    n = 1
    if prior_blockers:
        attempts.append(
            AttemptRecord(attempt=1, report=report,
                          verdict=rejected_verdict(prior_blockers))
        )
        n = 2
    final_verdict = (
        None
        if disposition is Disposition.VERIFICATION_FAILED
        else rejected_verdict(["still wrong"])
        if disposition is Disposition.REJECTED
        else approved_verdict()
    )
    attempts.append(AttemptRecord(attempt=n, report=report, verdict=final_verdict))
    return OrchestrationOutcome(
        request_id="eval-verifier",
        disposition=disposition,
        revisions_used=n - 1,
        attempts=attempts,
    )


def main() -> int:
    failures: list[str] = []
    stat_id, metric_id, spec = fetch_real_refs()
    request = make_request()
    honest = make_report(stat_id, metric_id, spec)

    with tempfile.TemporaryDirectory() as tmp:
        journal = Path(tmp) / "plant_memory.jsonl"
        memory = PlantEpisodicMemory(journal)

        # 1. Write gate ------------------------------------------------------
        ok_ep = memory.record_outcome(
            request,
            outcome_for(honest, Disposition.PENDING_HUMAN_APPROVAL,
                        prior_blockers=["claimed spec envelope [180.0, 250.0] does not match"]),
        )
        abstained_report = OptimizationReport(
            request_id="eval-verifier", status="abstained",
            abstain_reason="No telemetry for the requested machines and window.",
            summary="Cannot analyze without data.",
        )
        abst_ep = memory.record_outcome(
            request, outcome_for(abstained_report, Disposition.ABSTAINED)
        )
        rej = memory.record_outcome(request, outcome_for(honest, Disposition.REJECTED))
        vfail = memory.record_outcome(
            request, outcome_for(honest, Disposition.VERIFICATION_FAILED)
        )
        if not (ok_ep and abst_ep) or rej is not None or vfail is not None:
            failures.append(f"1: gate wrong (ok={bool(ok_ep)}, abst={bool(abst_ep)}, "
                            f"rej={rej}, vfail={vfail})")
        print("1) gate de escritura -> aceptados:", int(bool(ok_ep)) + int(bool(abst_ep)),
              "| rechazados:", int(rej is None) + int(vfail is None), "| total:", len(memory))

        # 2. Durability ------------------------------------------------------
        rehydrated = PlantEpisodicMemory(journal)
        if len(rehydrated) != len(memory):
            failures.append(f"2: rehydrated {len(rehydrated)} != {len(memory)}")
        print("2) durabilidad -> episodios re-hidratados:", len(rehydrated))

        # 3. Recall ranking ---------------------------------------------------
        # Plant an EX-01 episode to compete against.
        ex01 = memory._memory.record(
            context="line=LINE-A machines=EX-01 trigger=scheduled_review window=...",
            action="findings: screw speed nominal | recommendations: none",
            outcome="disposition=approved revisions=0 | Routine check.",
            success=True,
            tags=["LINE-A", "EX-01", "screw_speed", "scheduled_review", "approved"],
            metadata={"verifier_blockers": []},
        )
        memory._persist(ex01)
        hits = memory.recall(machine_id="EX-02", parameter="barrel_temperature", limit=3)
        if not hits or "EX-02" not in hits[0]["tags"]:
            failures.append(f"3: EX-02 episode not ranked first: {hits[:1]}")
        unrelated = memory.recall(machine_id="ZZ-99")
        if unrelated:
            failures.append(f"3: unrelated machine returned {len(unrelated)} episodes")
        print("3) ranking de recall -> primero:",
              hits[0]["tags"][:3] if hits else None, "| ZZ-99:", len(unrelated))

        # 4. Lessons travel ---------------------------------------------------
        lessons = hits[0]["verifier_blockers"] if hits else []
        if not any("does not match" in b for b in lessons):
            failures.append(f"4: blocker lesson missing from recall: {lessons}")
        print("4) lecciones en recall ->", lessons[:1])

        # 5. End-to-end via orchestrator (scripted creator/verifier) ----------
        import orchestrator as orch
        from agent import build_agent
        from verifier import build_verifier
        from eval_orchestrator import scripted, GENEROUS_VERDICT

        e2e_journal = Path(tmp) / "e2e_memory.jsonl"
        e2e_memory = PlantEpisodicMemory(e2e_journal)
        creator, verifier = build_agent(), build_verifier()

        async def run(creator_outputs, max_revisions=2):
            with creator.override(model=scripted(creator_outputs)), \
                 verifier.override(model=scripted([GENEROUS_VERDICT])):
                return await orch.orchestrate(
                    request, creator=creator, verifier=verifier,
                    max_revisions=max_revisions, memory=e2e_memory,
                )

        honest_json = honest.model_dump(mode="json")
        tampered_json = make_report(
            stat_id, metric_id, spec, tamper_envelope=True
        ).model_dump(mode="json")

        out_ok = asyncio.run(run([honest_json]))
        out_bad = asyncio.run(run([tampered_json], max_revisions=1))
        if len(e2e_memory) != 1:
            failures.append(f"5: expected exactly 1 episode, got {len(e2e_memory)}")
        if out_ok.disposition is not Disposition.PENDING_HUMAN_APPROVAL:
            failures.append(f"5: approved run got {out_ok.disposition}")
        if out_bad.disposition is not Disposition.REJECTED:
            failures.append(f"5: rejected run got {out_bad.disposition}")
        print("5) end-to-end -> aprobado escribió, rechazado no | episodios:",
              len(e2e_memory))

    if failures:
        print("\nEVAL FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nEVAL PASSED: el gate bloquea el poisoning, la memoria sobrevive "
          "reinicios, el recall prioriza bien y las lecciones del verificador viajan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
