"""Scan slots: one per 4H candle close (00, 04, ... 20 UTC). Shared by the run gate, the watchdog and the dashboard.

A slot is identified by the open time of the candle that closes at it (the candle the scan evaluates), exactly as
health.py records it in data/health/runs.csv (`expected_4h_candle_utc`).

  covered   a SUCCESSFUL run recorded that candle
  missed    no run of any outcome recorded it within `grace_min` after the close
"""
from __future__ import annotations

import datetime as dt

H4 = dt.timedelta(hours=4)


def last_close(now: dt.datetime) -> dt.datetime:
    """Most recent 4H close at or before `now`."""
    base = now.replace(minute=0, second=0, microsecond=0)
    return base - dt.timedelta(hours=base.hour % 4)


def slot_key(close: dt.datetime) -> str:
    """runs.csv key of the slot closing at `close` (open time of the candle that just closed)."""
    return (close - H4).isoformat(timespec="minutes")


def runs_for(runs: list[dict], key: str) -> list[dict]:
    return [r for r in runs if r.get("expected_4h_candle_utc") == key]


def covered(runs: list[dict], key: str) -> bool:
    return any(r["status"] == "success" for r in runs_for(runs, key))


def missed_slots(runs: list[dict], now: dt.datetime, hours: int = 48, grace_min: int = 120) -> list[str]:
    """Slot closes (ISO, UTC) in the last `hours` with no recorded run at all, once `grace_min` has passed.
    Only slots after the first recorded run count (earlier ones predate the automation)."""
    out = []
    starts = [dt.datetime.fromisoformat(r["started_utc"]) for r in runs if r.get("started_utc")]
    if not starts:
        return out
    since = min(starts)
    c = last_close(now)
    while c > now - dt.timedelta(hours=hours) and c > since:
        if now >= c + dt.timedelta(minutes=grace_min) and not runs_for(runs, slot_key(c)):
            out.append(c.isoformat(timespec="minutes"))
        c -= H4
    return out


def gate(runs: list[dict], now: dt.datetime, trigger: str) -> tuple[bool, str]:
    """Should this run proceed? Manual runs always do. Scheduled/watchdog runs skip a slot already covered."""
    if trigger == "manual":
        return True, "manual run"
    key = slot_key(last_close(now))
    if covered(runs, key):
        return False, f"slot {key} already covered by a successful run"
    return True, f"slot {key} not yet covered"


def watchdog_decision(runs: list[dict], active_runs: list[dict], now: dt.datetime,
                      wait_min: int = 40, max_attempts: int = 2) -> tuple[bool, str]:
    """Dispatch a scan only if the current slot is uncovered, nothing is queued/running, the regular schedule had
    `wait_min` minutes to start, and fewer than `max_attempts` runs were already made for this slot."""
    close = last_close(now)
    key = slot_key(close)
    if now < close + dt.timedelta(minutes=wait_min):
        return False, f"waiting: slot {key} closed less than {wait_min} min ago"
    if covered(runs, key):
        return False, f"slot {key} already covered"
    if active_runs:
        return False, f"a Data sync + scan run is already {active_runs[0].get('status')}"
    attempts = len(runs_for(runs, key))
    if attempts >= max_attempts:
        return False, f"slot {key}: {attempts} attempts already failed — not retrying (needs attention)"
    return True, f"slot {key} uncovered {int((now - close).total_seconds() // 60)} min after close"
