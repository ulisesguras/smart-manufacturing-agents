"""Eval case 04 — the orchestrator closes the loop correctly.

Scripted creators and verifiers (FunctionModel) drive five scenarios; the
deterministic prechecks run for real against the historian, so rejections
in these scenarios come from actual replayed evidence, not from stubs:

  1. HAPPY PATH   honest report, approving verifier
                  -> pending_human_approval, 0 revisions
  2. REVISION     tampered report first, honest second; the verifier LLM is
                  generous both times but prechecks block attempt #1
                  -> 1 revision, final pending_human_approval, and the
                  revision task handed to the creator must contain the
                  blocker text (feedback actually travels)
  3. KILL SWITCH  creator insists on the tampered report; max_revisions=1
                  -> rejected after 2 attempts, fail closed
  4. ABSTENTION   clean abstained report, approving verifier
                  -> abstained disposition
  5. FAIL CLOSED  verification itself raises
                  -> verification_failed, no verdict on the last attempt

Runs without an API key.
"""

from __future__ import annotations

import asyncio

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import orchestrator as orch
from agent import build_agent
from eval_verifier import fetch_real_refs, make_report, make_request
from schemas import Disposition, OptimizationReport
from verifier import build_verifier


def scripted(outputs: list[dict], seen_prompts: list[str] | None = None) -> FunctionModel:
    """A model that returns the next scripted structured output each run.

    Optionally records the last user prompt it received, so tests can
    assert what actually reached the agent.
    """
    state = {"i": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        if seen_prompts is not None:
            for part in messages[-1].parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    seen_prompts.append(content)
        args = outputs[min(state["i"], len(outputs) - 1)]
        state["i"] += 1
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    return FunctionModel(fn, model_name="scripted")


GENEROUS_VERDICT = {"request_id": "eval-verifier", "approved": True,
                    "confidence_score": 9, "issues": []}


async def run_scenarios() -> list[str]:
    failures: list[str] = []
    stat_id, metric_id, spec = fetch_real_refs()
    request = make_request()

    honest = make_report(stat_id, metric_id, spec).model_dump(mode="json")
    tampered = make_report(stat_id, metric_id, spec, tamper_envelope=True).model_dump(mode="json")
    abstained = OptimizationReport(
        request_id="eval-verifier", status="abstained",
        abstain_reason="No telemetry available for the requested machines and window.",
        summary="Cannot analyze without data.",
    ).model_dump(mode="json")

    creator, verifier = build_agent(), build_verifier()

    async def run(creator_outputs, *, max_revisions=2, prompts=None):
        with creator.override(model=scripted(creator_outputs, prompts)), \
             verifier.override(model=scripted([GENEROUS_VERDICT])):
            return await orch.orchestrate(
                request, creator=creator, verifier=verifier, max_revisions=max_revisions
            )

    # 1. Happy path
    out = await run([honest])
    if (out.disposition, out.revisions_used) != (Disposition.PENDING_HUMAN_APPROVAL, 0):
        failures.append(f"1: got {out.disposition}, revisions={out.revisions_used}")
    print("1) camino feliz ->", out.disposition.value, "| revisiones:", out.revisions_used)

    # 2. Revision loop with feedback delivery
    prompts: list[str] = []
    out = await run([tampered, honest], prompts=prompts)
    ok = (
        out.disposition is Disposition.PENDING_HUMAN_APPROVAL
        and out.revisions_used == 1
        and len(out.attempts) == 2
        and not out.attempts[0].verdict.approved
    )
    feedback_travelled = any(
        "REVISION TASK" in p and "does not match the real spec" in p for p in prompts
    )
    if not ok:
        failures.append(f"2: loop wrong: {out.disposition}, attempts={len(out.attempts)}")
    if not feedback_travelled:
        failures.append("2: verifier blockers never reached the creator's revision task")
    print("2) loop de revisión ->", out.disposition.value,
          "| intentos:", len(out.attempts), "| feedback viajó:", feedback_travelled)

    # 3. Kill switch
    out = await run([tampered], max_revisions=1)
    if (out.disposition, len(out.attempts)) != (Disposition.REJECTED, 2):
        failures.append(f"3: got {out.disposition}, attempts={len(out.attempts)}")
    print("3) kill switch ->", out.disposition.value, "| intentos:", len(out.attempts))

    # 4. Abstention
    out = await run([abstained])
    if out.disposition is not Disposition.ABSTAINED:
        failures.append(f"4: got {out.disposition}")
    print("4) abstención ->", out.disposition.value)

    # 5. Verifier failure -> fail closed
    original = orch.run_verification

    async def boom(*args, **kwargs):
        raise RuntimeError("verifier infrastructure down")

    orch.run_verification = boom
    try:
        out = await run([honest])
    finally:
        orch.run_verification = original
    if out.disposition is not Disposition.VERIFICATION_FAILED or out.attempts[-1].verdict is not None:
        failures.append(f"5: got {out.disposition}")
    print("5) verificador caído ->", out.disposition.value,
          "| veredicto final:", out.attempts[-1].verdict)

    return failures


def main() -> int:
    failures = asyncio.run(run_scenarios())
    if failures:
        print("\nEVAL FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nEVAL PASSED: el loop converge, el feedback viaja, el kill switch "
          "corta y nada sin verificar sale del orquestador.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
