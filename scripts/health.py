"""Record the health of this sync+scan run. Runs even when earlier steps failed (workflow `if: always()`).

Writes:
  data/health/runs.csv      append-only: one row per run (scheduled or manual), including failed ones
  data/health/latest.json   current state for the dashboard: last run, last SUCCESSFUL run, stale data, review queue
  data/health/STATUS.md     Hebrew operations summary over the recorded runs

Status of a run:
  success  sync and scan succeeded, the newest closed candle arrived, and no series is stale
  partial  scan succeeded but some series are stale or the sync reported errors (stale series are not scanned)
  failed   the sync or the scan failed; the dashboard keeps showing the last successful scan, marked as such
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIELDS = ["run_id", "event", "started_utc", "finished_utc", "status", "sync_outcome", "scan_outcome",
          "expected_4h_candle_utc", "btc_fresh", "freshness_attempts", "stale_series", "series_synced",
          "sync_errors", "universe_included", "review_queue", "signals_now", "watch_now", "journal_live_new",
          "journal_already_active", "episodes_ended", "notifications_queued", "start_delay_min", "duration_s",
          "code_commit"]
H4 = 4 * 3600


def find(pattern: str):
    hits = sorted(ROOT.glob(pattern))
    return hits[-1] if hits else None


def build_row(env: dict, now: dt.datetime) -> tuple[dict, dict]:
    run_id = env.get("GITHUB_RUN_ID") or "local"
    started = dt.datetime.fromisoformat(env["RUN_STARTED_UTC"]) if env.get("RUN_STARTED_UTC") else now
    sync_sum = find(f"reports/phase1/runs/*run-{run_id}/summary.json")
    scan_json = find(f"data/scan/*run-{run_id}.json")
    sync = json.loads(sync_sum.read_text()) if sync_sum else {}
    scan = json.loads(scan_json.read_text()) if scan_json else {}
    fresh = sync.get("freshness", {})
    stale = fresh.get("stale_series", [])
    sync_ok, scan_ok = env.get("SYNC_OUTCOME") == "success", env.get("SCAN_OUTCOME") == "success"
    if sync_ok and scan_ok and fresh.get("btc_fresh") and not stale:
        status = "success"
    elif scan_ok and scan:
        status = "partial"
    else:
        status = "failed"
    last_close = int(started.timestamp()) // H4 * H4
    stats = scan.get("run_stats", {})
    added = stats.get("journal_added", {})
    strategies = scan.get("strategies", [])
    row = {
        # schedule | watchdog | manual — which mechanism started the run
        "run_id": run_id, "event": env.get("RUN_TRIGGER") or env.get("GITHUB_EVENT_NAME", "local"),
        "started_utc": started.isoformat(timespec="seconds"), "finished_utc": now.isoformat(timespec="seconds"),
        "status": status, "sync_outcome": env.get("SYNC_OUTCOME", "unknown"),
        "scan_outcome": env.get("SCAN_OUTCOME", "unknown"),
        "expected_4h_candle_utc": dt.datetime.fromtimestamp(last_close - H4, dt.timezone.utc).isoformat(timespec="minutes"),
        "btc_fresh": fresh.get("btc_fresh"), "freshness_attempts": fresh.get("attempts"),
        "stale_series": len(stale), "series_synced": len(sync.get("sync", [])),
        "sync_errors": sum(1 for r in sync.get("sync", []) if r.get("error")) + len(sync.get("universe_meta", {}).get("fetch_errors", {})),
        "universe_included": sync.get("status_counts", {}).get("included"),
        "review_queue": sync.get("status_counts", {}).get("review"),
        "signals_now": sum(len(b["signals"]) for b in strategies) if scan else None,
        "watch_now": sum(len(b["watch"]) for b in strategies) if scan else None,
        "journal_live_new": added.get("live_new", 0), "journal_already_active": added.get("live_already_active", 0),
        "episodes_ended": stats.get("episodes_ended_new", 0), "notifications_queued": stats.get("notifications_queued", 0),
        "start_delay_min": round((started.timestamp() - last_close) / 60, 1),
        "duration_s": round((now - started).total_seconds()), "code_commit": (env.get("GITHUB_SHA") or "")[:12],
    }
    detail = {"stale_series": stale, "scan_run": scan.get("run"), "scan_generated_utc": scan.get("generated_utc"),
              "sync_run": sync.get("run"), "exchange_snapshot": sync.get("exchange_snapshot"),
              "dispatch_reason": env.get("DISPATCH_REASON") or None}
    return row, detail


def review_evidence() -> list[dict]:
    snap = find("data/universe/snapshots/*.csv")
    if not snap:
        return []
    with snap.open(newline="") as f:
        return [{k: r.get(k) for k in ("symbol", "status", "review_reason", "flags", "liquidity_warnings", "liquidity_hits",
                                       "liquidity_measurements", "liquidity_span_hours", "spread_pct",
                                       "depth_bid_1pct_usd", "depth_ask_1pct_usd", "median_quote_volume_30d")}
                for r in csv.DictReader(f) if r["status"] in ("review", "liquidity_suspended")]


def read_runs() -> list[dict]:
    p = ROOT / "data/health/runs.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def status_md(runs: list[dict], latest: dict) -> str:
    from radar import slots
    sched = [r for r in runs if r["event"] == "schedule"]
    missed = slots.missed_slots(runs, dt.datetime.now(dt.timezone.utc))
    ok = [r for r in runs if r["status"] == "success"]
    delays = [float(r["start_delay_min"]) for r in sched if r["start_delay_min"]]
    L = ["# מצב המערכת", "",
         f"- עודכן: {latest['updated_utc']}",
         f"- ריצה אחרונה: {latest['last_run']['started_utc']} · {latest['last_run']['event']} · **{latest['last_run']['status']}**",
         f"- ריצה מוצלחת אחרונה: {latest['last_success_utc'] or 'אין'}",
         f"- ריצות שנרשמו: {len(runs)} (מתוזמנות: {len(sched)}) · הצליחו: {len(ok)} · חלקיות: "
         f"{sum(r['status'] == 'partial' for r in runs)} · נכשלו: {sum(r['status'] == 'failed' for r in runs)}",
         f"- הפעלות: מתוזמנות {len(sched)} · גיבוי (watchdog) {sum(r['event'] == 'watchdog' for r in runs)} · "
         f"ידניות {sum(r['event'] in ('manual', 'workflow_dispatch') for r in runs)}",
         f"- חלונות סריקה שהוחמצו ב-48 השעות האחרונות: {', '.join(m[11:16] + ' UTC ' + m[5:10] for m in missed) or 'אין'}",
         f"- עיכוב התחלה של ריצות מתוזמנות אחרי סגירת נר 4H: "
         + (f"חציון {statistics.median(delays):.1f} דק׳, מקסימום {max(delays):.1f} דק׳" if delays else "אין עדיין ריצות מתוזמנות"),
         "", "| התחלה (UTC) | סוג | מצב | נר צפוי | סדרות לא עדכניות | התראות חדשות | שהסתיימו | משך (ש׳) |", "|---|---|---|---|---|---|---|---|"]
    for r in runs[-30:][::-1]:
        L.append(f"| {r['started_utc'][:16]} | {r['event']} | {r['status']} | {r['expected_4h_candle_utc'][11:16]} | "
                 f"{r['stale_series']} | {r['journal_live_new']} | {r['episodes_ended']} | {r['duration_s']} |")
    L += ["", "> הדוח נוצר אוטומטית בסוף כל ריצה, כולל ריצות שנכשלו."]
    return "\n".join(L) + "\n"


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    row, detail = build_row(dict(os.environ), now)
    hdir = ROOT / "data/health"
    hdir.mkdir(parents=True, exist_ok=True)
    path = hdir / "runs.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    runs = read_runs()
    last_ok = next((r for r in reversed(runs) if r["status"] == "success"), None)
    from radar import slots
    latest = {"updated_utc": now.isoformat(timespec="seconds"), "last_run": row, "detail": detail,
              "missed_slots_48h": slots.missed_slots(runs, now),
              "last_success_utc": last_ok["started_utc"] if last_ok else None,
              "review_evidence": review_evidence()}
    (hdir / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1))
    (hdir / "STATUS.md").write_text(status_md(runs, latest))
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
