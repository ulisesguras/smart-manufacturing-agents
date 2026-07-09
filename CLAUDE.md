# CLAUDE.md — Smart Manufacturing AI Agents

> **Merge policy for /init and regenerations**: if Claude Code proposes to
> regenerate or rewrite this file, MERGE with the existing content instead
> of replacing it. The six hard rules below are system invariants — keep
> them verbatim, at the top, in order. New sections (commands, style,
> discovered conventions) are welcome below them.

Rules go top to bottom: what breaks production first, preferences last.

## Hard rules (NEVER violate)

1. **Validators fail closed and are never relaxed.** If a schema validator
   in `schemas.py` / `scheduling_schemas.py` rejects something, fix the
   producer, not the validator. Loosening a gate to make a test pass is a
   production incident waiting to happen.
2. **Only the orchestrator writes episodic memory.** Agents get read-only
   recall tools. Never expose a write tool to a model; never call
   `PlantEpisodicMemory.record_outcome` outside `orchestrator.finalize`.
   REJECTED / VERIFICATION_FAILED outcomes never enter memory.
3. **Schedules come only from the solver; evidence comes only from the
   historian.** The LLM never invents assignments (must cite a
   reproducible `solution_id`) and never cites memory episodes as
   evidence (`EvidenceRef` kinds are historian-only by design).
4. **Nothing unverified leaves the orchestrator.** Every terminal path
   returns through `finalize()`. If you add a return path, wrap it.
5. **Human approval gates are computed, not asserted.** HIGH/CRITICAL
   risk, irreversible actions, and late schedules force
   `requires_human_approval=True` via `object.__setattr__` in validators.
   Do not add code paths that bypass this.
6. **Read-only by construction for plant data.** `historian_mcp_server`
   and `scheduling_mcp_server` expose no write tools. Actuation belongs in
   a future, separate, approval-gated server.

## Conventions

- Every new agent follows the same skeleton: `*_schemas.py` (contracts
  with fail-closed validators) -> `*_prompt.md` (hard rules top-down) ->
  MCP server (tools own the query logic; structured errors
  `{ok: false, error: {code, message}}`, never raw tracebacks) -> agent
  factory with `output_type=<Report>`, `retries=2`,
  `defer_model_check=True` -> deterministic prechecks -> eval file with
  positive AND negative cases.
- Data backends implement a Protocol (`HistorianBackend`) and are selected
  via env (`HISTORIAN_BACKEND=demo|csv`, `PLANT_CONFIG=<yaml>`). Plant
  models are configuration (YAML), never code.
- Evals are self-checking scripts returning exit code 0/1. A new behavior
  needs a passing case and a failing (negative) case.
- The demo backend synthesizes data for ANY window; citation-replay
  verification is only meaningful against finite real data (CSV backend).
  Sample plant CSVs go stale: regenerate with `make_sample_plant.py`
  before anything that queries "last N hours".
- Taxanomy (`remembrance` package) is imported ONLY in
  `episodic_memory.py`; when the upstream rename lands, that is the single
  file to touch.

## Commands

Paths below are POSIX (`.venv/bin/...`). On native Windows (PowerShell),
use `.venv\Scripts\python.exe` / `.venv\Scripts\pip.exe` instead; Git Bash
on Windows still uses the POSIX form shown here.

```bash
# environment
python -m venv .venv && .venv/bin/pip install -e .

# offline eval suite (no API key needed)
.venv/bin/python eval_verifier.py
.venv/bin/python eval_orchestrator.py
.venv/bin/python eval_memory.py
.venv/bin/python eval_scheduling.py

# live evals (require ANTHROPIC_API_KEY)
.venv/bin/python eval_ex02.py          # drift detection
.venv/bin/python eval_abstention.py    # clean abstention

# full pipelines (require ANTHROPIC_API_KEY)
.venv/bin/python orchestrator.py       # creator -> verifier -> revisions
.venv/bin/python scheduling_agent.py   # scheduling run + prechecks

# demo plant with fresh timestamps
.venv/bin/python make_sample_plant.py
HISTORIAN_BACKEND=csv PLANT_CONFIG=example_plant/plant_config.yaml \
    .venv/bin/python eval_ex02.py
```

## Architecture map

```
request -> orchestrator -> creator agent (PydanticAI, output_type gated)
              |                 |-- MCP: historian (demo|csv) [read-only]
              |                 |-- MCP: episodic memory      [read-only]
              |                 '-- MCP: scheduling solver    [read-only]
              |-> verifier = deterministic prechecks + skeptical LLM
              |       (code blockers override the model)
              |-> revision loop (clean context, max 2) -> fail closed
              '-> finalize(): gated episodic write (Taxanomy journal)
```

Dispositions: approved | pending_human_approval | abstained | rejected |
verification_failed. The last two never reach memory.
