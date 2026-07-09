"""Generate an example plant for the CSV backend.

Creates ``example_plant/`` with:
- ``plant_config.yaml``  two lines, three machines (one without telemetry,
  to preserve the abstention scenario on real-file mode)
- ``telemetry.csv``      48 h of 5-minute readings, with a planted
  temperature drift on EX-02 in the last 8 hours
- ``quality.csv``        hourly defect rate rising alongside the drift

Timestamps are relative to *now*, so the demo is always fresh. Re-run
before demos:

    .venv/bin/python make_sample_plant.py
    HISTORIAN_BACKEND=csv PLANT_CONFIG=example_plant/plant_config.yaml \
        .venv/bin/python eval_ex02.py
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "example_plant"

CONFIG = """\
plant_name: "Planta Ejemplo SA"
lines:
  - line_id: LINE-A
    machines:
      - machine_id: EX-01
        description: "Extrusora — sección de alimentación"
        telemetry: true
        parameters:
          - {name: screw_speed, unit: rpm, min_allowed: 40, nominal: 75, max_allowed: 110}
          - {name: feed_rate, unit: kg_per_hour, min_allowed: 80, nominal: 120, max_allowed: 160}
      - machine_id: EX-02
        description: "Extrusora — barril y matriz"
        telemetry: true
        parameters:
          - {name: barrel_temperature, unit: celsius, min_allowed: 180, nominal: 205, max_allowed: 230}
          - {name: die_pressure, unit: bar, min_allowed: 90, nominal: 140, max_allowed: 190}
  - line_id: LINE-B
    machines:
      - machine_id: MX-01
        description: "Mezclador — línea nueva, telemetría pendiente"
        telemetry: false
        parameters:
          - {name: agitator_speed, unit: rpm, min_allowed: 15, nominal: 45, max_allowed: 90}
data:
  telemetry_csv: telemetry.csv
  quality_csv: quality.csv
"""

NOMINALS = {
    ("EX-01", "screw_speed"): (75.0, 2.0),
    ("EX-01", "feed_rate"): (120.0, 3.0),
    ("EX-02", "barrel_temperature"): (205.0, 1.5),
    ("EX-02", "die_pressure"): (140.0, 4.0),
}

DRIFT_HOURS = 8
DRIFT_DELTA = 11.0  # degrees added to EX-02 barrel temperature, ramping in


def main(seed: int = 20260706) -> None:
    rng = random.Random(seed)
    OUT.mkdir(exist_ok=True)
    (OUT / "plant_config.yaml").write_text(CONFIG, encoding="utf-8")

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(hours=48)

    with (OUT / "telemetry.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "machine_id", "parameter", "value"])
        ts = start
        while ts <= now:
            hours_to_now = (now - ts).total_seconds() / 3600
            for (machine, param), (nominal, noise) in NOMINALS.items():
                value = rng.gauss(nominal, noise)
                if (
                    machine == "EX-02"
                    and param == "barrel_temperature"
                    and hours_to_now <= DRIFT_HOURS
                ):
                    ramp = 1.0 - hours_to_now / DRIFT_HOURS  # 0 -> 1
                    value += DRIFT_DELTA * ramp + rng.gauss(0, 1.5 * ramp)
                writer.writerow([ts.isoformat(), machine, param, f"{value:.2f}"])
            ts += timedelta(minutes=5)

    with (OUT / "quality.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "line_id", "metric", "value", "unit"])
        ts = start
        while ts <= now:
            hours_to_now = (now - ts).total_seconds() / 3600
            ramp = max(0.0, 1.0 - hours_to_now / DRIFT_HOURS)
            defect = max(0.2, rng.gauss(1.2 + 2.8 * ramp, 0.25))
            writer.writerow([ts.isoformat(), "LINE-A", "defect_rate", f"{defect:.2f}", "percent"])
            writer.writerow(
                [ts.isoformat(), "LINE-A", "first_pass_yield", f"{100 - defect - 0.5:.2f}", "percent"]
            )
            ts += timedelta(hours=1)

    print(f"example plant written to {OUT}/")


if __name__ == "__main__":
    main()
