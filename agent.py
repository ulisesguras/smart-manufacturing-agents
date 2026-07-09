"""Process Optimization Agent — PydanticAI wiring.

Glues the three existing pieces together:

- ``system_prompt.md``  -> loaded verbatim as the agent's instructions
- ``schemas.py``        -> ``OptimizationReport`` enforced as ``output_type``;
                           an answer that fails validation triggers an
                           automatic retry with the validation error fed back
- ``historian_mcp_server.py`` -> attached as an MCP toolset

Toolset modes
-------------
``historian_inprocess()``  imports the FastMCP server object and runs it in
the same process. No subprocess, no transport — ideal for evals and tests.

``historian_stdio()``      spawns the server as a subprocess over stdio,
which is how it runs in production alongside other MCP servers.

The agent code is identical in both modes; only the toolset differs.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

from schemas import OptimizationReport, OptimizationRequest

_HERE = Path(__file__).parent

DEFAULT_MODEL = os.environ.get(
    "PROCESS_OPT_MODEL", "anthropic:claude-sonnet-4-6"
)


def load_system_prompt() -> str:
    return (_HERE / "system_prompt.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Toolsets
# --------------------------------------------------------------------------

def historian_inprocess() -> MCPToolset:
    """In-process historian: same event loop, no subprocess.

    Used by evals so runs are fast, deterministic and debuggable.
    """
    from historian_mcp_server import mcp as historian_server

    return MCPToolset(historian_server, id="plant-historian")


def historian_stdio(python_bin: str = "python") -> MCPToolset:
    """Production wiring: the historian runs as its own process over stdio."""
    transport = StdioTransport(
        command=python_bin,
        args=[str(_HERE / "historian_mcp_server.py")],
        cwd=str(_HERE),
    )
    return MCPToolset(transport, id="plant-historian")


def memory_inprocess() -> MCPToolset:
    """In-process episodic memory (read-only recall tools)."""
    from memory_mcp_server import mcp as memory_server

    return MCPToolset(memory_server, id="episodic-memory")


# --------------------------------------------------------------------------
# Agent factory
# --------------------------------------------------------------------------

def build_agent(
    toolset: MCPToolset | None = None,
    model: str = DEFAULT_MODEL,
    memory_toolset: MCPToolset | None = None,
) -> Agent[None, OptimizationReport]:
    """Build the Process Optimization agent.

    ``output_type=OptimizationReport`` means every schema validator in
    ``schemas.py`` acts as a runtime guardrail: out-of-spec setpoints,
    understated risk on irreversible actions and dirty abstentions all
    surface as validation errors that force the model to correct itself
    (up to ``retries`` times) instead of reaching the Verifier.

    ``memory_toolset`` (optional) adds read-only episodic recall. The agent
    can consult past verified episodes as priors; it can never write them.
    """
    toolsets = [toolset or historian_inprocess()]
    if memory_toolset is not None:
        toolsets.append(memory_toolset)
    return Agent(
        model,
        name="process-optimization",
        instructions=load_system_prompt(),
        output_type=OptimizationReport,
        toolsets=toolsets,
        retries=2,
        # The API key is a runtime concern: evals and tests should be able
        # to construct (and override) the agent without credentials.
        defer_model_check=True,
    )


def format_task(request: OptimizationRequest) -> str:
    """Serialize the orchestrator's request as the user message."""
    return (
        "New optimization task. Analyze and report.\n\n"
        f"```json\n{request.model_dump_json(indent=2)}\n```"
    )


async def run_optimization(
    agent: Agent[None, OptimizationReport],
    request: OptimizationRequest,
) -> OptimizationReport:
    result = await agent.run(format_task(request))
    return result.output


if __name__ == "__main__":
    # Smoke run against the demo backend. Requires ANTHROPIC_API_KEY.
    import asyncio
    from datetime import datetime, timedelta, timezone

    from schemas import AnalysisWindow, TriggerType

    now = datetime.now(timezone.utc)
    request = OptimizationRequest(
        request_id="smoke-001",
        line_id="LINE-A",
        machine_ids=["EX-01", "EX-02", "WD-01"],
        trigger=TriggerType.DRIFT_ALARM,
        window=AnalysisWindow(start=now - timedelta(hours=8), end=now - timedelta(minutes=5)),
        operator_notes="Drift alarm raised on the extrusion line during night shift.",
    )
    report = asyncio.run(run_optimization(build_agent(), request))
    print(report.model_dump_json(indent=2))
