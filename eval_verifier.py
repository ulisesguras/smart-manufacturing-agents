"""Eval case 03 — the Verifier catches what the schema cannot.

Three doctored reports exercise the deterministic prechecks:

  A. HONEST     — real citations (re-fetched live from the historian) and
                  the true spec envelope. Prechecks must come back clean.
  B. INVENTED   — one citation that never came from the historian.
                  Precheck must raise a blocker.
  C. TAMPERED   — the creator "lies" about the spec envelope (claims
                  spec_max=250 to sneak a 240 C recommendation past schema
                  validation, which only checks against *claimed* bounds).
                  Precheck must raise blockers by comparing against the
                  REAL spec (max 230).

Plus the merge rule: a generous LLM verdict cannot override code blockers
(facts outrank judgment), and the merged verdict stays schema-valid.

The LLM layer itself needs an API key; everything here runs without one,
except the final plumbing check which uses TestModel.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from schemas import (
    EvidenceRef,
    Finding,
    OptimizationReport,
    OptimizationRequest,
    AnalysisWindow,
    ParameterRecommendation,
    RiskLevel,
    TriggerType,
    VerifierVerdict,
)
from verifier import deterministic_prechecks, merge_verdicts


def _window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (
        (now - timedelta(hours=8)).isoformat(),
        (now - timedelta(minutes=5)).isoformat(),
    )


def fetch_real_refs() -> tuple[str, str, dict]:
    """Get a genuine stat_id, metric_id and the real EX-02 temp spec."""
    import historian_mcp_server as historian

    start, end = _window()
    stat = historian.get_sensor_stats("EX-02", start, end, "barrel_temperature")["stats"][0]
    metric = historian.get_quality_metrics("LINE-A", start, end)["metrics"][0]
    spec = next(
        s
        for s in historian.get_machine_spec("EX-02")["specs"]
        if s["parameter"] == "barrel_temperature"
    )
    return stat["stat_id"], metric["metric_id"], spec


def make_report(
    stat_id: str,
    metric_id: str,
    spec: dict,
    *,
    invent: str | None = None,  # None | "machine" | "window"
    tamper_envelope: bool = False,
) -> OptimizationReport:
    if invent == "machine":
        # Non-existent machine: any backend fails to reproduce this.
        stat_id = "stat::EX-99::barrel_temperature::20260101T0000-20260101T0800"
    elif invent == "window":
        # Plausible machine/parameter, but a window with no real data.
        # Only a finite-data backend (CSV) can catch this one — the demo
        # backend synthesizes data for ANY window by construction.
        stat_id = "stat::EX-02::barrel_temperature::20200101T0000-20200101T0800"
    claimed_min = spec["min_allowed"]
    claimed_max = 250.0 if tamper_envelope else spec["max_allowed"]
    recommended = 240.0 if tamper_envelope else spec["nominal"]

    return OptimizationReport(
        request_id="eval-verifier",
        status="ok",
        findings=[
            Finding(
                finding_id="f1",
                statement="EX-02 barrel temperature runs above nominal with widened variance.",
                evidence=[
                    EvidenceRef(kind="sensor_stat", ref_id=stat_id, note="recent stats"),
                    EvidenceRef(kind="quality_metric", ref_id=metric_id, note="defect rate"),
                ],
                confidence=0.85,
            )
        ],
        recommendations=[
            ParameterRecommendation(
                machine_id="EX-02",
                parameter="barrel_temperature",
                unit="celsius",
                current_value=215.0,
                recommended_value=recommended,
                spec_min=claimed_min,
                spec_max=claimed_max,
                rationale_finding_ids=["f1"],
                expected_effect="stabilize temperature",
                risk=RiskLevel.HIGH,
                reversible=True,
            )
        ],
        summary="Drift on EX-02; recommend adjustment pending approval.",
    )


def make_request() -> OptimizationRequest:
    start, end = _window()
    return OptimizationRequest(
        request_id="eval-verifier",
        line_id="LINE-A",
        machine_ids=["EX-02"],
        trigger=TriggerType.DRIFT_ALARM,
        window=AnalysisWindow(
            start=datetime.fromisoformat(start), end=datetime.fromisoformat(end)
        ),
    )


def main() -> int:
    failures: list[str] = []
    stat_id, metric_id, spec = fetch_real_refs()

    # A. Honest report -> clean prechecks
    honest = make_report(stat_id, metric_id, spec)
    issues_a = deterministic_prechecks(honest)
    if issues_a:
        failures.append(f"A: honest report raised issues: {[i.description for i in issues_a]}")
    print("A) reporte honesto -> prechecks:", "limpio ✓" if not issues_a else issues_a)

    # B1. Invented citation, non-existent machine -> any backend blocks it
    invented_machine = make_report(stat_id, metric_id, spec, invent="machine")
    issues_b1 = deterministic_prechecks(invented_machine)
    if not any("could not be reproduced" in i.description for i in issues_b1):
        failures.append("B1: invented machine citation was not blocked")
    print("B1) cita con máquina inexistente ->", [i.description[:70] for i in issues_b1])

    # B2. Invented citation, plausible window -> only a real-data backend
    # catches it. The demo backend synthesizes data for any window, so this
    # case runs against the CSV backend with the example plant files.
    import historian_mcp_server as historian
    from pathlib import Path

    plant = Path(__file__).parent / "example_plant" / "plant_config.yaml"
    # Always regenerate: the sample CSVs carry timestamps relative to their
    # generation time, and this eval queries "last 8 hours" — stale files
    # would produce a false no_data and break the honest-citation setup.
    import make_sample_plant

    make_sample_plant.main()
    from csv_backend import CSVBackend

    original_backend = historian.backend
    try:
        historian.backend = CSVBackend(plant)
        # Real refs must come from the same backend under test.
        csv_stat, csv_metric, csv_spec = fetch_real_refs()
        invented_window = make_report(csv_stat, csv_metric, csv_spec, invent="window")
        issues_b2 = deterministic_prechecks(invented_window)
        if not any("could not be reproduced" in i.description for i in issues_b2):
            failures.append("B2: plausible-window citation was not blocked on real data")
        print("B2) cita con ventana sin datos (backend CSV) ->",
              [i.description[:70] for i in issues_b2])
    finally:
        historian.backend = original_backend

    # C. Tampered envelope: schema-valid but physically out of spec
    tampered = make_report(stat_id, metric_id, spec, tamper_envelope=True)
    issues_c = deterministic_prechecks(tampered)
    envelope_caught = any("does not match the real spec" in i.description for i in issues_c)
    value_caught = any("outside the REAL spec envelope" in i.description for i in issues_c)
    if not (envelope_caught and value_caught):
        failures.append(f"C: tampered envelope not fully caught: {[i.description for i in issues_c]}")
    print("C) envelope adulterado ->", [i.description[:70] for i in issues_c])

    # D. Merge: generous LLM verdict cannot override code blockers
    generous = VerifierVerdict(request_id="eval-verifier", approved=True, confidence_score=9)
    merged = merge_verdicts(generous, issues_c)
    if merged.approved:
        failures.append("D: code blockers did not force rejection")
    merged_roundtrip = VerifierVerdict.model_validate(merged.model_dump())  # schema-valid
    print(
        "D) fusión código-sobre-modelo -> approved:",
        merged.approved,
        f"| issues: {len(merged_roundtrip.issues)} | score: {merged.confidence_score}",
    )

    # E. Regression: memoized DemoBackend stats must not drift between two
    # reads of the same window, even seconds apart (creator reads once,
    # verifier replays later — they must see identical numbers).
    import historian_mcp_server as historian

    start, end = _window()
    first_stats = historian.get_sensor_stats("EX-02", start, end, "barrel_temperature")["stats"]
    second_stats = historian.get_sensor_stats("EX-02", start, end, "barrel_temperature")["stats"]
    first_metrics = historian.get_quality_metrics("LINE-A", start, end)["metrics"]
    second_metrics = historian.get_quality_metrics("LINE-A", start, end)["metrics"]
    if first_stats != second_stats:
        failures.append(f"E: sensor_stats drifted across reads: {first_stats} vs {second_stats}")
    if first_metrics != second_metrics:
        failures.append(f"E: quality_metrics drifted across reads: {first_metrics} vs {second_metrics}")
    print(
        "E) lecturas repetidas de la misma ventana -> estable:",
        first_stats == second_stats and first_metrics == second_metrics,
    )

    if failures:
        print("\nEVAL FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("\nEVAL PASSED: los prechecks reproducen citas, detectan specs adulteradas "
          "y los blockers de código dominan la fusión.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
