# Smart Manufacturing AI Agents

Verified, memory-backed AI agents for industrial process optimization and
production scheduling. Built on [PydanticAI](https://ai.pydantic.dev),
[MCP](https://modelcontextprotocol.io) and
[Taxanomy](https://github.com/ulisesguras/remembrance) (nine-layer agent
memory).

**Design stance: an agent that touches a factory must prove everything.**
Every claim cites reproducible evidence, every schedule is a re-runnable
solver output, every report is audited by an independent verifier, and
every risky action waits for a human signature. Nothing unverified leaves
the pipeline; nothing unverified enters memory.

## What it does today

- **Process Optimization agent** — analyzes telemetry and quality data,
  diagnoses drift, proposes setpoint changes within spec envelopes. Every
  finding cites historian references (`stat::…`, `metric::…`, `spec::…`)
  that a verifier replays; if the data is insufficient it abstains
  cleanly instead of guessing.
- **Production Scheduling agent** — plans the horizon by invoking a
  deterministic EDD solver exposed as an MCP tool, then interprets the
  result (late orders, unschedulable orders, utilization risks). The LLM
  cannot invent a schedule: reports must carry a `solution_id` the
  verifier reproduces by re-running the solver.
- **Verifier** — two layers: deterministic prechecks (citation replay,
  real-spec envelope comparison, schedule reproduction, physics
  re-validation) and a skeptical LLM reviewer for judgment calls. Code
  blockers override the model.
- **Orchestrator** — creator/verifier loop with clean-context revisions
  (max 2), five auditable terminal dispositions, and fail-closed behavior
  on verifier errors.
- **Episodic memory (Taxanomy)** — durable JSONL journal of verified
  outcomes only (write-gated in code: rejected work cannot poison future
  recalls). Agents recall past incidents — including the verifier
  blockers earned along the way — as priors, never as evidence.

## Safety properties (enforced, not promised)

| Property | Enforced by |
|---|---|
| Setpoints inside spec envelopes | schema validator + verifier re-check vs real spec |
| No invented citations | deterministic replay against the historian |
| No invented schedules | solver re-run + `solution_id` comparison |
| Risky/irreversible actions gated by humans | computed flag, not model-settable |
| No memory poisoning | writes only in orchestrator code, verified outcomes only |
| Fail closed | rejected/errored runs never emit results nor memories |

## Quickstart

```bash
python -m venv .venv && .venv/bin/pip install -e .

# Offline eval suite — no API key required
.venv/bin/python eval_verifier.py
.venv/bin/python eval_orchestrator.py
.venv/bin/python eval_memory.py
.venv/bin/python eval_scheduling.py

# Live pipeline against the demo plant (drift planted on machine EX-02)
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python orchestrator.py
```

## Connect a real plant in minutes

Every industrial system exports tabular files. Onboarding is one YAML plus
two CSVs — no client-side integration:

```bash
.venv/bin/python make_sample_plant.py     # generates example_plant/
HISTORIAN_BACKEND=csv PLANT_CONFIG=example_plant/plant_config.yaml \
    .venv/bin/python eval_ex02.py
```

The data layer is a Protocol (`HistorianBackend`); native connectors
(SQL, OPC UA) plug in behind the same interface without touching agents,
prompts or schemas.

## Project layout

```
schemas.py                  optimization contracts (fail-closed validators)
scheduling_schemas.py       scheduling contracts
agent.py / scheduling_agent.py        agent factories (PydanticAI)
system_prompt.md / scheduling_prompt.md / verifier_prompt.md
historian_mcp_server.py     plant data tools (demo + CSV backends)
scheduling_mcp_server.py    orders/capacities + deterministic EDD solver
memory_mcp_server.py        read-only episodic recall (Taxanomy)
csv_backend.py              universal tabular adapter + plant YAML config
episodic_memory.py          durable, write-gated memory on Taxanomy
verifier.py                 prechecks + skeptical reviewer + merge rule
orchestrator.py             creator->verifier loop, dispositions, memory gate
eval_*.py                   six self-checking eval suites
make_sample_plant.py        demo plant generator (fresh timestamps)
```

## Status & roadmap

Honest status: pre-revenue, no external deployments yet. All six eval
suites pass; live-model evals validated against the demo plant. Roadmap,
in order of pull (built when a concrete client needs it, not before):

1. Supply-chain resilience specialist (third agent)
2. Shadow-mode pilot tooling (daily report + human grading loop)
3. Native data connectors (SQL, OPC UA) behind `HistorianBackend`
4. Approval-gated actuation server (separate, write-capable MCP server)
5. Quantum-assisted scheduling strategy (QAOA) behind the same
   `solve_schedule` interface, classical-fallback-first

## License

Apache License 2.0 — see [LICENSE](LICENSE).
