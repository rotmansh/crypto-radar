"""Append the result of a live dashboard check to data/health/site-checks.csv (append-only)."""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["run_id", "event", "checked_utc", "outcome", "published_run_matches", "problems", "timings"]


def main() -> int:
    result_path, outcome = Path(sys.argv[1]), sys.argv[2]
    res = json.loads(result_path.read_text()) if result_path.exists() else {}
    row = {"run_id": os.environ.get("GITHUB_RUN_ID", "local"), # the mechanism that started the run (schedule | watchdog | external_cron | manual), not the GitHub event:
           # watchdog and routine runs arrive as workflow_dispatch
           "event": os.environ.get("RUN_TRIGGER") or os.environ.get("GITHUB_EVENT_NAME", "local"),
           "checked_utc": res.get("checked_utc"), "outcome": outcome,
           "published_run_matches": res.get("published_run_matches"),
           "problems": " | ".join(res.get("problems", ["no result file"] if not res else [])),
           "timings": " ".join(res.get("timings", []))}
    path = ROOT / "data/health/site-checks.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
