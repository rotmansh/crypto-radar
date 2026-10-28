"""Run gate / watchdog decision for Data sync + scan. Prints `key=value` lines for $GITHUB_OUTPUT.

    python scripts/slot_gate.py gate <trigger>                 # first job of data-sync.yml; also appends the
                                                               # decision to data/health/gate-decisions.csv
    python scripts/slot_gate.py record                         # sync job: re-append $GATE_DECISION on latest main
    python scripts/slot_gate.py watchdog <active_runs.json>    # scan-watchdog.yml
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import slots  # noqa: E402

ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
GATE_FIELDS = ["run_id", "trigger", "decided_utc", "slot", "run", "reason", "dispatch_reason"]


def log_gate(row: dict) -> None:
    """Append-only record of every gate decision, so skipped duplicates and their trigger stay visible."""
    p = ROOT / "data/health/gate-decisions.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GATE_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


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
        row = {"run_id": os.environ.get("GITHUB_RUN_ID", "local"), "trigger": argv[1],
               "decided_utc": now.isoformat(timespec="seconds"), "slot": slots.slot_key(slots.last_close(now)),
               "run": ok, "reason": why, "dispatch_reason": os.environ.get("DISPATCH_REASON", "")}
        log_gate(row)
        print(f"run={'true' if ok else 'false'}")
        print(f"decision={json.dumps(row, ensure_ascii=False)}")
    elif argv[0] == "record":
        # the sync job starts from the latest main, without the gate job's file: re-append the gate's own row
        log_gate(json.loads(os.environ["GATE_DECISION"]))
        return 0
    else:
        active = [r for r in json.loads(Path(argv[1]).read_text()) if r.get("status") in ACTIVE]
        ok, why = slots.watchdog_decision(runs, active, now)
        print(f"dispatch={'true' if ok else 'false'}")
    print(f"reason={why}")
    print(why, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
