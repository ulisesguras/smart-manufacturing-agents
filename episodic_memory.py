"""Plant episodic memory — Taxanomy integration.

Uses Taxanomy's ``EpisodicMemory``/``Episode`` as the in-process engine
(the package is still importable as ``remembrance``; when the rename lands
upstream, this module is the only place to touch) and adds the two layers
a plant deployment needs on top:

1. **Durability**: an append-only JSONL journal. Episodes re-hydrate on
   startup via ``Episode.from_dict``, and every accepted write is flushed
   as one JSON line — the memory survives restarts and is trivially
   inspectable/auditable with standard tools.

2. **Gated writes (memory-poisoning prevention)**: nothing enters memory
   unless it survived verification. Only ``APPROVED``,
   ``PENDING_HUMAN_APPROVAL`` and ``ABSTAINED`` outcomes are recorded;
   ``REJECTED`` and ``VERIFICATION_FAILED`` are refused at the gate. Bad
   diagnoses must not corrupt future retrievals.

What gets remembered per episode:
- context: line, machines, trigger, analysis window
- action: findings and recommendations (compact, human-readable)
- outcome: disposition + revision cost
- metadata: the *lessons* — every verifier blocker raised across attempts,
  plus structured copies of findings/recommendations
- tags: machine ids, parameters, line, trigger, disposition — the recall keys

Memory is a PRIOR for the creator agent, never evidence: the EvidenceRef
schema only admits historian kinds, so an episode cannot be cited even by
a confused model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from remembrance.memory.episodic import Episode, EpisodicMemory  # Taxanomy

from schemas import Disposition, OptimizationRequest, OrchestrationOutcome

WRITABLE_DISPOSITIONS = {
    Disposition.APPROVED,
    Disposition.PENDING_HUMAN_APPROVAL,
    Disposition.ABSTAINED,
}


class PlantEpisodicMemory:
    """Durable, write-gated plant memory on top of Taxanomy."""

    def __init__(self, journal_path: Path | str):
        self.journal_path = Path(journal_path)
        self._memory = EpisodicMemory()
        self._hydrate()

    # -- persistence ---------------------------------------------------------

    def _hydrate(self) -> None:
        if not self.journal_path.exists():
            return
        episodes: list[Episode] = []
        with self.journal_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    episodes.append(Episode.from_dict(json.loads(line)))
        # Rebuild the in-process engine preserving ids and timestamps.
        self._memory._episodes = episodes
        self._memory._id_counter = len(episodes)

    def _persist(self, episode: Episode) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")

    # -- gated write ----------------------------------------------------------

    def record_outcome(
        self, request: OptimizationRequest, outcome: OrchestrationOutcome
    ) -> Episode | None:
        """Write one episode IF the outcome survived verification.

        Returns the stored episode, or None when the gate refuses it.
        """
        if outcome.disposition not in WRITABLE_DISPOSITIONS:
            return None

        report = outcome.final_report
        parameters = sorted(
            {r.parameter for r in report.recommendations}
            | {
                ev.ref_id.split("::")[2]
                for f in report.findings
                for ev in f.evidence
                if ev.kind == "sensor_stat" and len(ev.ref_id.split("::")) == 4
            }
        )
        # Lessons: every blocker any attempt earned on the way here.
        blockers = [
            issue.description
            for attempt in outcome.attempts
            if attempt.verdict
            for issue in attempt.verdict.issues
            if issue.severity == "blocker"
        ]

        context = (
            f"line={request.line_id} machines={','.join(request.machine_ids)} "
            f"trigger={request.trigger.value} "
            f"window={request.window.start.isoformat()}..{request.window.end.isoformat()}"
        )
        if report.status == "abstained":
            action = f"abstained: {report.abstain_reason}"
        else:
            finding_bits = "; ".join(f.statement for f in report.findings)
            rec_bits = "; ".join(
                f"{r.machine_id}/{r.parameter}: {r.current_value} -> "
                f"{r.recommended_value} {r.unit} (risk={r.risk.value})"
                for r in report.recommendations
            )
            action = f"findings: {finding_bits or 'none'} | recommendations: {rec_bits or 'none'}"

        episode = self._memory.record(
            context=context,
            action=action,
            outcome=(
                f"disposition={outcome.disposition.value} "
                f"revisions={outcome.revisions_used} | {report.summary}"
            ),
            success=outcome.disposition is not Disposition.ABSTAINED,
            tags=[
                request.line_id,
                *request.machine_ids,
                *parameters,
                request.trigger.value,
                outcome.disposition.value,
            ],
            metadata={
                "request_id": outcome.request_id,
                "revisions_used": outcome.revisions_used,
                "verifier_blockers": blockers,
                "findings": [f.model_dump(mode="json") for f in report.findings],
                "recommendations": [
                    r.model_dump(mode="json") for r in report.recommendations
                ],
            },
        )
        self._persist(episode)
        return episode

    # -- recall ----------------------------------------------------------------

    def recall(
        self,
        machine_id: str | None = None,
        parameter: str | None = None,
        keyword: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Relevance-ranked recall: machine match > parameter match > recency.

        Returns compact dicts ready for the MCP tool boundary.
        """
        candidates = self._memory.search(keyword) if keyword else list(
            self._memory._episodes
        )

        def score(ep: Episode) -> tuple[int, float]:
            s = 0
            if machine_id and machine_id in ep.tags:
                s += 2
            if parameter and parameter in ep.tags:
                s += 1
            return (s, ep.timestamp)

        if machine_id or parameter:
            candidates = [ep for ep in candidates if score(ep)[0] > 0]
        ranked = sorted(candidates, key=score, reverse=True)[: max(1, min(limit, 20))]
        return [
            {
                "episode_id": ep.episode_id,
                "when_unix": ep.timestamp,
                "context": ep.context,
                "action": ep.action,
                "outcome": ep.outcome,
                "tags": ep.tags,
                "verifier_blockers": ep.metadata.get("verifier_blockers", []),
            }
            for ep in ranked
        ]

    def __len__(self) -> int:
        return len(self._memory)
