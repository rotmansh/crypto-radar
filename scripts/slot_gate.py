"""Run gate / watchdog decision for Data sync + scan. Prints `key=value` lines for $GITHUB_OUTPUT.

    python scripts/slot_gate.py gate <trigger>                 # first job of data-sync.yml
    python scripts/slot_gate.py watchdog <active_runs.json>    # scan-watchdog.yml
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import slots  # noqa: E402

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def read_runs() -> list[dict]:
    p = ROOT / "data/health/runs.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def main(argv: list[str]) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    runs = read_runs()
    if argv[0] == "gate":
        ok, why = slots.gate(runs, now, argv[1])
        print(f"run={'true' if ok else 'false'}")
    else:
        active = [r for r in json.loads(Path(argv[1]).read_text()) if r.get("status") in ACTIVE]
        ok, why = slots.watchdog_decision(runs, active, now)
        print(f"dispatch={'true' if ok else 'false'}")
    print(f"reason={why}")
    print(why, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
