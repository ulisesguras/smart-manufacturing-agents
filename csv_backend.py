"""CSV/tabular backend — the universal onboarding adapter.

Every industrial system (SCADA, historian, MES, ERP, or a supervisor's
spreadsheet) can export tabular files. This backend turns a plant into two
CSVs plus one YAML config, which makes it the zero-integration entry point
for pilots: "export me the last 30 days" instead of touching client IT.

Plant config (YAML)
-------------------
    plant_name: "Planta Ejemplo SA"
    lines:
      - line_id: LINE-A
        machines:
          - machine_id: EX-01
            description: "Extrusora — alimentación"
            telemetry: true          # false => spec exists, no data yet
            parameters:
              - name: screw_speed
                unit: rpm
                min_allowed: 40
                nominal: 75
                max_allowed: 110
    data:
      telemetry_csv: telemetry.csv   # timestamp,machine_id,parameter,value
      quality_csv: quality.csv       # timestamp,line_id,metric,value,unit

CSV expectations
----------------
- ``timestamp``: ISO 8601. Naive timestamps are assumed UTC.
- Rows with unknown machines/parameters are ignored (and counted), so a
  raw SCADA export with extra tags does not break onboarding.
- Aggregation (mean/std/min/max over a window) happens here, in code the
  agent never sees — the tool owns the query logic.

Swap-in
-------
    HISTORIAN_BACKEND=csv PLANT_CONFIG=/path/plant_config.yaml \
        python historian_mcp_server.py

No agent, schema or prompt changes required: same MCP tools, same
contracts, different plant.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from schemas import AnalysisWindow, MachineSpec, QualityMetric, SensorStat


def _parse_ts(raw: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


@dataclass
class _MachineCfg:
    machine_id: str
    line_id: str
    description: str
    telemetry: bool
    # parameter -> (unit, min_allowed, nominal, max_allowed)
    parameters: dict[str, tuple[str, float, float, float]] = field(default_factory=dict)


class CSVBackend:
    """HistorianBackend over client-exported tabular files."""

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path)
        self.base_dir = self.config_path.parent
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

        self.plant_name: str = raw.get("plant_name", "unnamed plant")
        self._machines: dict[str, _MachineCfg] = {}
        self._lines: dict[str, list[str]] = {}

        for line in raw.get("lines", []):
            line_id = line["line_id"]
            self._lines[line_id] = []
            for m in line.get("machines", []):
                params = {
                    p["name"]: (
                        str(p["unit"]),
                        float(p["min_allowed"]),
                        float(p["nominal"]),
                        float(p["max_allowed"]),
                    )
                    for p in m.get("parameters", [])
                }
                cfg = _MachineCfg(
                    machine_id=m["machine_id"],
                    line_id=line_id,
                    description=m.get("description", m["machine_id"]),
                    telemetry=bool(m.get("telemetry", True)),
                    parameters=params,
                )
                self._machines[cfg.machine_id] = cfg
                self._lines[line_id].append(cfg.machine_id)

        data = raw.get("data", {})
        self._telemetry_csv = self.base_dir / data.get("telemetry_csv", "telemetry.csv")
        self._quality_csv = self.base_dir / data.get("quality_csv", "quality.csv")
        self._telemetry_cache: list[tuple[datetime, str, str, float]] | None = None
        self._quality_cache: list[tuple[datetime, str, str, float, str]] | None = None
        self.ignored_rows = {"telemetry": 0, "quality": 0}

    # -- lazy CSV loading ---------------------------------------------------

    def _telemetry(self) -> list[tuple[datetime, str, str, float]]:
        if self._telemetry_cache is None:
            rows: list[tuple[datetime, str, str, float]] = []
            if self._telemetry_csv.exists():
                with self._telemetry_csv.open(newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        ts = _parse_ts(row.get("timestamp", ""))
                        mid = (row.get("machine_id") or "").strip()
                        param = (row.get("parameter") or "").strip()
                        cfg = self._machines.get(mid)
                        try:
                            value = float(row.get("value", ""))
                        except (TypeError, ValueError):
                            value = None
                        if ts is None or value is None or cfg is None or param not in cfg.parameters:
                            self.ignored_rows["telemetry"] += 1
                            continue
                        rows.append((ts, mid, param, value))
            rows.sort(key=lambda r: r[0])
            self._telemetry_cache = rows
        return self._telemetry_cache

    def _quality(self) -> list[tuple[datetime, str, str, float, str]]:
        if self._quality_cache is None:
            rows: list[tuple[datetime, str, str, float, str]] = []
            if self._quality_csv.exists():
                with self._quality_csv.open(newline="", encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        ts = _parse_ts(row.get("timestamp", ""))
                        line_id = (row.get("line_id") or "").strip()
                        metric = (row.get("metric") or "").strip()
                        unit = (row.get("unit") or "").strip() or "unitless"
                        try:
                            value = float(row.get("value", ""))
                        except (TypeError, ValueError):
                            value = None
                        if ts is None or value is None or line_id not in self._lines or not metric:
                            self.ignored_rows["quality"] += 1
                            continue
                        rows.append((ts, line_id, metric, value, unit))
            rows.sort(key=lambda r: r[0])
            self._quality_cache = rows
        return self._quality_cache

    # -- HistorianBackend protocol -------------------------------------------

    def list_machines(self, line_id: str) -> list[dict]:
        return [
            {
                "machine_id": mid,
                "description": self._machines[mid].description,
                "line_id": line_id,
            }
            for mid in self._lines.get(line_id, [])
        ]

    def machine_specs(self, machine_id: str) -> list[MachineSpec]:
        cfg = self._machines.get(machine_id)
        if cfg is None:
            return []
        return [
            MachineSpec(
                spec_id=f"spec::{machine_id}::{param}",
                machine_id=machine_id,
                parameter=param,
                unit=unit,
                min_allowed=lo,
                max_allowed=hi,
                nominal=nom,
            )
            for param, (unit, lo, nom, hi) in cfg.parameters.items()
        ]

    def sensor_stats(
        self, machine_id: str, parameter: str | None, window: AnalysisWindow
    ) -> list[SensorStat]:
        cfg = self._machines.get(machine_id)
        if cfg is None or not cfg.telemetry:
            return []
        wanted = [parameter] if parameter else list(cfg.parameters)
        wid = f"{window.start:%Y%m%dT%H%M}-{window.end:%Y%m%dT%H%M}"
        out: list[SensorStat] = []
        for param in wanted:
            if param not in cfg.parameters:
                continue
            values = [
                v
                for ts, mid, p, v in self._telemetry()
                if mid == machine_id and p == param and window.start <= ts <= window.end
            ]
            if not values:
                continue
            unit = cfg.parameters[param][0]
            out.append(
                SensorStat(
                    stat_id=f"stat::{machine_id}::{param}::{wid}",
                    machine_id=machine_id,
                    parameter=param,
                    unit=unit,
                    mean=round(statistics.fmean(values), 3),
                    std=round(statistics.pstdev(values) if len(values) > 1 else 0.0, 4),
                    minimum=round(min(values), 3),
                    maximum=round(max(values), 3),
                    sample_count=len(values),
                    window=window,
                )
            )
        return out

    def quality_metrics(self, line_id: str, window: AnalysisWindow) -> list[QualityMetric]:
        if line_id not in self._lines:
            return []
        wid = f"{window.start:%Y%m%dT%H%M}-{window.end:%Y%m%dT%H%M}"
        grouped: dict[tuple[str, str], list[float]] = {}
        for ts, lid, metric, value, unit in self._quality():
            if lid == line_id and window.start <= ts <= window.end:
                grouped.setdefault((metric, unit), []).append(value)
        return [
            QualityMetric(
                metric_id=f"metric::{line_id}::{metric}::{wid}",
                line_id=line_id,
                metric=metric,
                value=round(statistics.fmean(values), 3),
                unit=unit,
                window=window,
            )
            for (metric, unit), values in sorted(grouped.items())
        ]
