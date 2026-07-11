"""Plant Historian MCP server (read-only).

First MCP server of the system: exposes aggregated telemetry, machine
specs and quality metrics to the Process Optimization Agent.

Design decisions
----------------
- "Put the LLM's work inside the tool": the agent never writes SQL or
  aggregates raw readings. It asks for the statistic it needs and the
  tool owns the query logic, validation and error shape.
- READ-ONLY by construction. There is no tool here that writes anywhere.
  Actuation lives in a separate, approval-gated MCP server.
- Every tool returns either a payload validated against the shared
  Pydantic schemas, or a structured error dict — never a raw traceback.
  A structured error lets the agent decide to retry or abstain.
- The data layer is a swappable interface (``HistorianBackend``). The
  bundled ``DemoBackend`` generates a deterministic synthetic dataset for
  development and evals; replace it with a PI/InfluxDB/TimescaleDB client
  without touching the tools.

Run
---
    pip install "mcp[cli]" pydantic
    python historian_mcp_server.py            # stdio transport

Register in a client (e.g. .mcp.json / claude.json):
    {"mcpServers": {"plant-historian": {
        "command": "python", "args": ["historian_mcp_server.py"]}}}
"""

from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Protocol

from mcp.server.fastmcp import FastMCP

from schemas import (
    AnalysisWindow,
    MachineSpec,
    QualityMetric,
    SensorStat,
)

mcp = FastMCP("plant-historian")

MAX_WINDOW_HOURS = 24 * 7  # bounded queries: one week max per call


# --------------------------------------------------------------------------
# Data layer (swappable)
# --------------------------------------------------------------------------

class HistorianBackend(Protocol):
    def list_machines(self, line_id: str) -> list[dict]: ...
    def sensor_stats(
        self, machine_id: str, parameter: str | None, window: AnalysisWindow
    ) -> list[SensorStat]: ...
    def machine_specs(self, machine_id: str) -> list[MachineSpec]: ...
    def quality_metrics(self, line_id: str, window: AnalysisWindow) -> list[QualityMetric]: ...


class DemoBackend:
    """Deterministic synthetic plant: one extrusion line, three machines.

    Values are derived from hashes of (machine, parameter, window), so the
    same query always returns the same numbers — which makes agent evals
    reproducible. EX-02 exhibits a deliberate temperature drift so there
    is something real to diagnose.
    """

    LINE = "LINE-A"
    LINE_B = "LINE-B"  # commissioned line, telemetry NOT yet connected
    MACHINES = {
        "EX-01": "Extruder — feed section",
        "EX-02": "Extruder — barrel and die",
        "WD-01": "Winder",
    }
    MACHINES_B = {
        "MX-01": "Mixer — new line, telemetry pending commissioning",
    }
    # Machines whose sensors actually report to the historian. MX-01 has a
    # spec sheet but no data: the trap scenario for the abstention eval.
    TELEMETRY_COMMISSIONED = {"EX-01", "EX-02", "WD-01"}
    # parameter -> (unit, min_allowed, nominal, max_allowed)
    SPECS: dict[str, dict[str, tuple[str, float, float, float]]] = {
        "EX-01": {
            "screw_speed": ("rpm", 40.0, 75.0, 110.0),
            "feed_rate": ("kg_per_hour", 80.0, 120.0, 160.0),
        },
        "EX-02": {
            "barrel_temperature": ("celsius", 180.0, 205.0, 230.0),
            "die_pressure": ("bar", 90.0, 140.0, 190.0),
        },
        "WD-01": {
            "line_tension": ("newton", 20.0, 35.0, 55.0),
        },
        "MX-01": {
            "agitator_speed": ("rpm", 15.0, 45.0, 90.0),
            "batch_temperature": ("celsius", 20.0, 60.0, 85.0),
        },
    }

    def __init__(self) -> None:
        # Freezes wall-clock-dependent values (drift/recency) per query key,
        # so a verifier replaying the same window later sees identical
        # numbers instead of a value that crept between the two reads.
        self._sensor_stats_cache: dict[tuple[str, str, str, str], SensorStat] = {}
        self._quality_metrics_cache: dict[tuple[str, str, str], list[QualityMetric]] = {}

    @staticmethod
    def _seed(*parts: str) -> float:
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF  # stable float in [0, 1]

    def list_machines(self, line_id: str) -> list[dict]:
        per_line = {self.LINE: self.MACHINES, self.LINE_B: self.MACHINES_B}
        machines = per_line.get(line_id, {})
        return [
            {"machine_id": mid, "description": desc, "line_id": line_id}
            for mid, desc in machines.items()
        ]

    def machine_specs(self, machine_id: str) -> list[MachineSpec]:
        specs = []
        for param, (unit, lo, nom, hi) in self.SPECS.get(machine_id, {}).items():
            specs.append(
                MachineSpec(
                    spec_id=f"spec::{machine_id}::{param}",
                    machine_id=machine_id,
                    parameter=param,
                    unit=unit,
                    min_allowed=lo,
                    max_allowed=hi,
                    nominal=nom,
                )
            )
        return specs

    def sensor_stats(
        self, machine_id: str, parameter: str | None, window: AnalysisWindow
    ) -> list[SensorStat]:
        if machine_id not in self.TELEMETRY_COMMISSIONED:
            return []  # spec sheet exists, but no sensor reports yet
        params = self.SPECS.get(machine_id, {})
        selected = [parameter] if parameter else list(params)
        stats: list[SensorStat] = []
        for param in selected:
            if param not in params:
                continue
            cache_key = (
                machine_id,
                param,
                window.start.isoformat(),
                window.end.isoformat(),
            )
            cached = self._sensor_stats_cache.get(cache_key)
            if cached is not None:
                stats.append(cached)
                continue
            unit, lo, nom, hi = params[param]
            noise = self._seed(machine_id, param, window.start.isoformat())
            # Deliberate drift on EX-02 barrel temperature: mean creeps up
            # and variance widens in recent windows.
            hours_ago = max(
                0.0,
                (datetime.now(timezone.utc) - window.end).total_seconds() / 3600,
            )
            drift = 0.0
            spread = (hi - lo) * 0.02
            if machine_id == "EX-02" and param == "barrel_temperature":
                recency = math.exp(-hours_ago / 24)  # stronger in recent data
                drift = 12.0 * recency
                spread = (hi - lo) * (0.02 + 0.05 * recency)
            mean = min(hi, nom + drift + (noise - 0.5) * spread)
            std = spread * (0.8 + 0.4 * noise)
            stat = SensorStat(
                stat_id=(
                    f"stat::{machine_id}::{param}::"
                    f"{window.start:%Y%m%dT%H%M}-{window.end:%Y%m%dT%H%M}"
                ),
                machine_id=machine_id,
                parameter=param,
                unit=unit,
                mean=round(mean, 2),
                std=round(std, 3),
                minimum=round(mean - 2 * std, 2),
                maximum=round(min(hi + 2.0, mean + 2 * std), 2),
                sample_count=int(
                    (window.end - window.start).total_seconds() // 10
                )
                or 1,
                window=window,
            )
            self._sensor_stats_cache[cache_key] = stat
            stats.append(stat)
        return stats

    def quality_metrics(self, line_id: str, window: AnalysisWindow) -> list[QualityMetric]:
        if line_id != self.LINE:
            return []
        cache_key = (line_id, window.start.isoformat(), window.end.isoformat())
        cached = self._quality_metrics_cache.get(cache_key)
        if cached is not None:
            return cached
        noise = self._seed(line_id, "defect_rate", window.start.isoformat())
        hours_ago = max(
            0.0, (datetime.now(timezone.utc) - window.end).total_seconds() / 3600
        )
        recency = math.exp(-hours_ago / 24)
        defect = 1.2 + 2.5 * recency + noise * 0.4  # rises with the drift
        wid = f"{window.start:%Y%m%dT%H%M}-{window.end:%Y%m%dT%H%M}"
        metrics = [
            QualityMetric(
                metric_id=f"metric::{line_id}::defect_rate::{wid}",
                line_id=line_id,
                metric="defect_rate",
                value=round(defect, 2),
                unit="percent",
                window=window,
            ),
            QualityMetric(
                metric_id=f"metric::{line_id}::first_pass_yield::{wid}",
                line_id=line_id,
                metric="first_pass_yield",
                value=round(100.0 - defect - noise, 2),
                unit="percent",
                window=window,
            ),
        ]
        self._quality_metrics_cache[cache_key] = metrics
        return metrics


def make_backend() -> HistorianBackend:
    """Select the data layer via environment, without touching the tools.

    HISTORIAN_BACKEND=demo   (default) deterministic synthetic plant
    HISTORIAN_BACKEND=csv    client-exported tabular files; requires
                             PLANT_CONFIG=/path/to/plant_config.yaml
    """
    kind = os.environ.get("HISTORIAN_BACKEND", "demo").lower()
    if kind == "csv":
        from csv_backend import CSVBackend

        config = os.environ.get("PLANT_CONFIG")
        if not config:
            raise RuntimeError("HISTORIAN_BACKEND=csv requires PLANT_CONFIG=<yaml path>")
        return CSVBackend(config)
    if kind != "demo":
        raise RuntimeError(f"unknown HISTORIAN_BACKEND '{kind}' (use 'demo' or 'csv')")
    return DemoBackend()


backend: HistorianBackend = make_backend()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _error(code: str, message: str) -> dict:
    """Structured error: the agent can read it and choose retry/abstain."""
    return {"ok": False, "error": {"code": code, "message": message}}


def _parse_window(start_iso: str, end_iso: str) -> AnalysisWindow | dict:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        window = AnalysisWindow(start=start, end=end)
    except (ValueError, TypeError) as exc:
        return _error("invalid_window", str(exc))
    if window.end - window.start > timedelta(hours=MAX_WINDOW_HOURS):
        return _error(
            "window_too_large",
            f"max window is {MAX_WINDOW_HOURS} hours; split the query",
        )
    return window


# --------------------------------------------------------------------------
# Tools (all read-only)
# --------------------------------------------------------------------------

@mcp.tool()
def list_machines(line_id: str) -> dict:
    """List the machines on a production line.

    Args:
        line_id: Line identifier, e.g. "LINE-A".
    """
    machines = backend.list_machines(line_id)
    if not machines:
        return _error("unknown_line", f"no machines found for line '{line_id}'")
    return {"ok": True, "machines": machines}


@mcp.tool()
def get_machine_spec(machine_id: str) -> dict:
    """Get the allowed operating envelope (min/nominal/max) for every
    parameter of a machine. Call this BEFORE recommending any setpoint.

    Args:
        machine_id: Machine identifier, e.g. "EX-02".
    """
    specs = backend.machine_specs(machine_id)
    if not specs:
        return _error("unknown_machine", f"no specs for machine '{machine_id}'")
    return {"ok": True, "specs": [s.model_dump(mode="json") for s in specs]}


@mcp.tool()
def get_sensor_stats(
    machine_id: str,
    start_iso: str,
    end_iso: str,
    parameter: str | None = None,
) -> dict:
    """Get aggregated sensor statistics (mean/std/min/max) for a machine
    over a time window. Returns one entry per parameter; each carries a
    stat_id you must cite as evidence.

    Args:
        machine_id: Machine identifier, e.g. "EX-02".
        start_iso: Window start, ISO 8601 (UTC assumed if naive).
        end_iso: Window end, ISO 8601.
        parameter: Optional single parameter, e.g. "barrel_temperature".
    """
    window = _parse_window(start_iso, end_iso)
    if isinstance(window, dict):
        return window
    stats = backend.sensor_stats(machine_id, parameter, window)
    if not stats:
        return _error(
            "no_data",
            f"no stats for machine '{machine_id}'"
            + (f", parameter '{parameter}'" if parameter else ""),
        )
    return {"ok": True, "stats": [s.model_dump(mode="json") for s in stats]}


@mcp.tool()
def get_quality_metrics(line_id: str, start_iso: str, end_iso: str) -> dict:
    """Get quality KPIs (defect rate, first-pass yield) for a line over a
    time window. Each metric carries a metric_id you must cite as evidence.

    Args:
        line_id: Line identifier, e.g. "LINE-A".
        start_iso: Window start, ISO 8601.
        end_iso: Window end, ISO 8601.
    """
    window = _parse_window(start_iso, end_iso)
    if isinstance(window, dict):
        return window
    metrics = backend.quality_metrics(line_id, window)
    if not metrics:
        if not backend.list_machines(line_id):
            return _error("unknown_line", f"no such line '{line_id}'")
        return _error(
            "no_data",
            f"line '{line_id}' exists but has no quality data in this window "
            "(telemetry may not be commissioned yet)",
        )
    return {"ok": True, "metrics": [m.model_dump(mode="json") for m in metrics]}


if __name__ == "__main__":
    mcp.run()
