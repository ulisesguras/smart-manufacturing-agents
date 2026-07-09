"""Episodic Memory MCP server (read-only, by design).

The creator agent can RECALL past episodes; it can never write them.
Writes happen exclusively in the orchestrator, in code, after
verification — the write gate is not something a model can be talked out
of. This asymmetry (agent reads, orchestrator writes) is the memory
architecture's core safety property.

Run standalone:
    MEMORY_JOURNAL=plant_memory.jsonl python memory_mcp_server.py
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from episodic_memory import PlantEpisodicMemory

mcp = FastMCP("episodic-memory")

_journal = Path(os.environ.get("MEMORY_JOURNAL", "plant_memory.jsonl"))
memory = PlantEpisodicMemory(_journal)


@mcp.tool()
def recall_episodes(
    machine_id: str | None = None,
    parameter: str | None = None,
    keyword: str | None = None,
    limit: int = 5,
) -> dict:
    """Recall past verified episodes about this plant, most relevant first.

    Use BEFORE diagnosing: a similar past incident tells you what was found,
    what was recommended, and what the verifier objected to along the way.
    Episodes are context/priors — they are NOT evidence and cannot be cited
    in findings; evidence must still come from the historian.

    Args:
        machine_id: Filter by machine, e.g. "EX-02".
        parameter: Filter by parameter, e.g. "barrel_temperature".
        keyword: Free-text search over context/action/outcome.
        limit: Max episodes to return (1-20, default 5).
    """
    episodes = memory.recall(
        machine_id=machine_id, parameter=parameter, keyword=keyword, limit=limit
    )
    if not episodes:
        return {
            "ok": True,
            "episodes": [],
            "note": "no relevant episodes in memory yet",
        }
    return {"ok": True, "episodes": episodes}


@mcp.tool()
def memory_stats() -> dict:
    """How much verified history this plant's memory holds."""
    return {"ok": True, "episode_count": len(memory), "journal": str(_journal)}


if __name__ == "__main__":
    mcp.run()
