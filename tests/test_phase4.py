"""Phase 4: lifecycle of live alerts, notification outbox, run health, freshness gate, schedule definition.
All candle data here is TEST FIXTURES."""
import csv
import dataclasses
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

import health
import sync_data
from radar import lifecycle, notify
from radar.strategies import BollingerSqueeze, NONE, SIGNAL, WATCH
from test_strategies import b_frame, put

ROOT = Path(__file__).resolve().parent.parent
B = BollingerSqueeze()


# ---------------------------------------------------------------- lifecycle

def frame_with_states(states, closes=None):
    """A real B-strategy evaluation whose `state` column is then set to a chosen sequence (fixture)."""
    df = b_frame(len(states) + 5).iloc[5:].copy()
    df["open_time"] = df.index.as_unit("ms").asi8
    if closes is not None:
        df["close"] = closes
    ev = B.evaluate(df)
    ev["state"] = states
    return df, ev


def row(df, i, kind="WATCH", **kw):
    r = {"signal_id": f"id{i}{kind}", "origin": "live_new", "symbol": "XUSDT", "timeframe": "1d", "strategy": B.id,
         "strategy_version": B.version, "type": kind, "episode_started_utc": pd.Timestamp(df.index[i]).isoformat(),
         "candle_open_utc": pd.Timestamp(df.index[i]).isoformat(), "stop": "", "detected_at_utc": "x"}
    r.update(kw)
    return r


def follow(states, i, kind="WATCH", closes=None, **kw):
    df, ev = frame_with_states(states, closes)
    return lifecycle.follow(row(df, i, kind, **kw), {(B.id, "1d", "XUSDT"): (B, df, ev)})


def test_watch_new_active_and_ended_reasons():
    n = 30
    s = [NONE] * n
    assert follow(s[:-1] + [WATCH], n - 1)["status"] == "new"
    assert follow(s[:-3] + [WATCH] * 3, n - 3)["status"] == "active"
    ended = follow(s[:-4] + [WATCH, WATCH, SIGNAL, NONE], n - 4)
    assert ended["status"] == "ended" and ended["reason"] == "became_signal"
    left = follow(s[:-3] + [WATCH, "NOT_IN_UNIVERSE", NONE], n - 3)
    assert left["reason"] == "left_universe"
    closes = np.full(n, 102.0)
    closes[-2] = 90.0  # below the middle band (100) that cancels a B watch
    inv = follow(s[:-3] + [WATCH, NONE, NONE], n - 3, closes=closes)
    assert inv["reason"] == "invalidated"
    cond = follow(s[:-3] + [WATCH, NONE, NONE], n - 3)
    assert cond["reason"] == "conditions_ended"


def test_signal_active_then_stopped_or_window_elapsed():
    n = 30
    s = [NONE] * n
    s_new = s[:-1] + [SIGNAL]
    assert follow(s_new, n - 1, "SIGNAL", stop="95")["status"] == "new"
    assert follow(s[:-4] + [SIGNAL, NONE, NONE, NONE], n - 4, "SIGNAL", stop="95")["status"] == "active"
    closes = np.full(n, 100.0)
    closes[-2] = 94.0
    st = follow(s[:-4] + [SIGNAL, NONE, NONE, NONE], n - 4, "SIGNAL", closes=closes, stop="95")
    assert st["status"] == "ended" and st["reason"] == "closed_below_stop"
    old = follow(s[:-15] + [SIGNAL] + [NONE] * 14, n - 15, "SIGNAL", stop="1")
    assert old["status"] == "ended" and old["reason"] == "window_elapsed"


def test_unfollowable_alerts_are_unknown_not_guessed():
    df, ev = frame_with_states([NONE] * 29 + [WATCH])
    r = row(df, 29)
    assert lifecycle.follow(r, {})["status"] == "unknown"                      # series not available
    assert lifecycle.follow(dict(r, strategy_version="0.9"), {(B.id, "1d", "XUSDT"): (B, df, ev)})["status"] == "unknown"


# ---------------------------------------------------------------- notifications

def jrow(sid, origin="live_new", kind="WATCH"):
    return {"signal_id": sid, "origin": origin, "type": kind, "symbol": "XUSDT", "strategy": "B-BBSQ",
            "timeframe": "4h", "candle_open_utc": "2026-10-27T04:00:00Z", "ref_price": 1.0, "stop": None,
            "target": None, "watch_invalidation": 0.9, "high_risk": False, "detected_at_utc": "2026-10-27T08:07:00+00:00"}


def test_outbox_queues_new_alerts_once_and_never_sends(tmp_path):
    rows = [jrow("a"), jrow("b", origin="live_already_active"), jrow("c", kind="SIGNAL")]
    assert notify.queue(rows, tmp_path, "2026-10") == 2           # already-active alerts are not news
    assert notify.queue(rows, tmp_path, "2026-10") == 0           # never twice
    msgs = [json.loads(x) for x in (tmp_path / "2026-10.jsonl").read_text().splitlines()]
    assert all(m["status"] == "queued_not_sent" and m["channel"] is None for m in msgs)
    assert "אינו ייעוץ השקעות" in msgs[0]["text_he"]
    with pytest.raises(notify.NotificationsDisabled):
        notify.send(msgs[0], {"notifications": {"enabled": False}})
    with pytest.raises(notify.NotificationsDisabled):
        notify.send(msgs[0], {"notifications": {"enabled": True, "channels": ["x"]}})  # no channel implemented


def test_settings_keep_notifications_disabled():
    from radar import config
    assert config.load_settings()["notifications"]["enabled"] is False


# ---------------------------------------------------------------- health

def write_run_files(root, run_id, fresh=True, stale=(), scan=True):
    (root / "reports/phase1/runs" / f"2026-10-27-run-{run_id}").mkdir(parents=True)
    (root / "reports/phase1/runs" / f"2026-10-27-run-{run_id}" / "summary.json").write_text(json.dumps({
        "run": run_id, "freshness": {"btc_fresh": fresh, "attempts": 1, "stale_series": list(stale)},
        "sync": [{"symbol": "BTCUSDT", "error": None}], "status_counts": {"included": 47, "review": 4},
        "universe_meta": {"fetch_errors": {}}}))
    if scan:
        (root / "data/scan").mkdir(parents=True, exist_ok=True)
        (root / "data/scan" / f"2026-10-27-run-{run_id}.json").write_text(json.dumps({
            "run": run_id, "strategies": [{"signals": [1], "watch": [1, 2]}],
            "run_stats": {"journal_added": {"live_new": 2}, "episodes_ended_new": 1, "notifications_queued": 2}}))


@pytest.mark.parametrize("sync,scan_,fresh,stale,has_scan,expected", [
    ("success", "success", True, [], True, "success"),
    ("success", "success", True, [{"symbol": "X"}], True, "partial"),
    ("failure", "success", False, [], True, "partial"),
    ("failure", "failure", False, [], False, "failed"),
    ("success", "skipped", True, [], False, "failed"),
])
def test_run_status_is_never_success_on_failure(tmp_path, monkeypatch, sync, scan_, fresh, stale, has_scan, expected):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    write_run_files(tmp_path, "7", fresh, stale, has_scan)
    env = {"GITHUB_RUN_ID": "7", "GITHUB_EVENT_NAME": "schedule", "SYNC_OUTCOME": sync, "SCAN_OUTCOME": scan_,
           "RUN_STARTED_UTC": "2026-10-27T12:09:00+00:00"}
    row, _ = health.build_row(env, dt.datetime(2026, 10, 27, 12, 12, tzinfo=dt.timezone.utc))
    assert row["status"] == expected
    assert row["expected_4h_candle_utc"] == "2026-10-27T08:00+00:00" and row["start_delay_min"] == 9.0


def test_health_main_appends_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "ROOT", tmp_path)
    write_run_files(tmp_path, "8")
    for k, v in {"GITHUB_RUN_ID": "8", "GITHUB_EVENT_NAME": "schedule", "SYNC_OUTCOME": "success",
                 "SCAN_OUTCOME": "success", "RUN_STARTED_UTC": "2026-10-27T12:09:00+00:00"}.items():
        monkeypatch.setenv(k, v)
    health.main()
    health.main()
    runs = list(csv.DictReader((tmp_path / "data/health/runs.csv").open()))
    assert len(runs) == 2 and runs[-1]["status"] == "success"
    latest = json.loads((tmp_path / "data/health/latest.json").read_text())
    assert latest["last_success_utc"] and latest["last_run"]["journal_live_new"] == 2
    assert "מתוזמנות: 2" in (tmp_path / "data/health/STATUS.md").read_text()


# ---------------------------------------------------------------- freshness gate & exchange snapshot

class R:
    def __init__(self, sym, itv, last, appended=0, error=None):
        self.symbol, self.interval, self.appended, self.error = sym, itv, appended, error
        self.report = type("Rep", (), {"last": last})()


def test_sync_waits_for_the_newest_candle_then_succeeds(monkeypatch):
    now = 1_761_552_420_000  # fixed "now"
    monkeypatch.setattr(sync_data, "now_ms", lambda: now)
    exp = {i: sync_data.iso(sync_data.expected_last_open(i, now)) for i in ("1d", "4h")}
    old = {i: sync_data.iso(sync_data.expected_last_open(i, now) - (86_400_000 if i == "1d" else 14_400_000)) for i in exp}
    passes = iter([[R("BTCUSDT", "1d", exp["1d"], 1), R("BTCUSDT", "4h", old["4h"], 0)],
                   [R("BTCUSDT", "1d", exp["1d"], 0), R("BTCUSDT", "4h", exp["4h"], 1)]])
    monkeypatch.setattr(sync_data, "sync_histories", lambda *a, **k: next(passes))
    monkeypatch.setattr(sync_data.time, "sleep", lambda s: None)
    results, fr = sync_data.sync_until_fresh(["BTCUSDT"], {}, None, "")
    assert fr["btc_fresh"] and fr["attempts"] == 2 and fr["stale_series"] == []
    assert {(r.interval, r.appended) for r in results} == {("1d", 1), ("4h", 1)}


def test_sync_reports_failure_if_candle_never_arrives(monkeypatch):
    now = 1_761_552_420_000
    monkeypatch.setattr(sync_data, "now_ms", lambda: now)
    old = sync_data.iso(sync_data.expected_last_open("4h", now) - 14_400_000)
    monkeypatch.setattr(sync_data, "sync_histories", lambda *a, **k: [R("BTCUSDT", "4h", old)])
    monkeypatch.setattr(sync_data.time, "sleep", lambda s: None)
    _, fr = sync_data.sync_until_fresh(["BTCUSDT"], {}, None, "", attempts=3)
    assert not fr["btc_fresh"] and fr["attempts"] == 3 and fr["stale_series"][0]["symbol"] == "BTCUSDT"


def test_exchange_snapshot_saved_only_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_data, "ROOT", tmp_path)
    rows = [{"symbol": "AUSDT", "base": "A", "quote": "USDT", "status": "TRADING"}]
    assert sync_data.write_exchange_snapshot(rows, "t1").startswith("saved")
    assert sync_data.write_exchange_snapshot(rows, "t2") == "unchanged since t1.csv"
    rows2 = rows + [{"symbol": "BUSDT", "base": "B", "quote": "USDT", "status": "BREAK"}]
    assert sync_data.write_exchange_snapshot(rows2, "t3").startswith("saved")
    assert sorted(p.name for p in (tmp_path / "data/universe/exchange").glob("*.csv")) == ["t1.csv", "t3.csv"]


# ---------------------------------------------------------------- schedule definition

def wf(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_schedule_runs_after_each_4h_close_and_never_overlaps():
    w = wf("data-sync.yml")
    on = w[True] if True in w else w["on"]  # YAML 1.1 reads the key `on` as boolean True
    cron = on["schedule"][0]["cron"]
    minute, hours = cron.split()[:2]
    assert int(minute) == 23 and sorted(int(h) for h in hours.split(",")) == [0, 4, 8, 12, 16, 20]
    assert "workflow_dispatch" in on
    assert w["concurrency"]["cancel-in-progress"] is False
    assert w["jobs"]["sync"]["needs"] == "gate" and w["jobs"]["sync"]["if"] == "needs.gate.outputs.run == 'true'"
    assert "'skipped'" in w["jobs"]["site"]["if"]  # a gated-off run publishes nothing
    assert on["workflow_dispatch"]["inputs"]["trigger"]["options"] == ["manual", "watchdog", "claude_routine", "external_cron"]
    steps = {s.get("id"): s for s in w["jobs"]["sync"]["steps"] if s.get("id")}
    assert steps["health"]["if"] == "always()"
    assert "always()" in w["jobs"]["site"]["if"]  # the dashboard is republished to show a failure
    assert w["jobs"]["verify"]["needs"] == ["gate", "sync", "site"]
    # the live-check record names the mechanism (watchdog/routine runs are workflow_dispatch events to GitHub)
    rec = next(st for st in w["jobs"]["verify"]["steps"] if st.get("name") == "Record and commit the check")
    assert rec["env"]["RUN_TRIGGER"] == "${{ needs.gate.outputs.trigger }}"
    skip = next(st for st in w["jobs"]["gate"]["steps"] if st.get("name") == "Record a skipped run")
    assert "check_append_only.py" in skip["run"] and "gate-decisions.csv" in skip["run"]


def test_site_check_records_the_trigger_not_the_github_event(tmp_path, monkeypatch):
    import record_site_check
    monkeypatch.setattr(record_site_check, "ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUN_TRIGGER", "claude_routine")
    (tmp_path / "r.json").write_text('{"checked_utc": "t", "published_run_matches": true}')
    monkeypatch.setattr("sys.argv", ["x", str(tmp_path / "r.json"), "success"])
    record_site_check.main()
    assert ",claude_routine," in (tmp_path / "data/health/site-checks.csv").read_text()


def test_gate_decisions_are_logged_including_skips(tmp_path, monkeypatch, capsys):
    import slot_gate
    monkeypatch.setattr(slot_gate, "ROOT", tmp_path)
    monkeypatch.setenv("DISPATCH_REASON", "routine fired")
    slot_gate.main(["gate", "claude_routine"])
    out = capsys.readouterr().out
    assert "run=true" in out and "decision=" in out
    rows = (tmp_path / "data/health/gate-decisions.csv").read_text().splitlines()
    assert rows[0].startswith("run_id,trigger") and ",claude_routine," in rows[1] and "routine fired" in rows[1]
    # the sync job re-appends the same decision on the latest main
    monkeypatch.setenv("GATE_DECISION", next(l for l in out.splitlines() if l.startswith("decision="))[9:])
    slot_gate.main(["record"])
    assert len((tmp_path / "data/health/gate-decisions.csv").read_text().splitlines()) == 3


def test_watchdog_only_dispatches_and_cannot_write():
    w = wf("scan-watchdog.yml")
    on = w[True] if True in w else w["on"]
    assert on["schedule"][0]["cron"].split()[0] == "47"            # a different minute than the main schedule
    assert w["permissions"] == {"contents": "read", "actions": "write"}  # cannot push data
    runs = " ".join(st.get("run", "") for st in w["jobs"]["check"]["steps"])
    assert "gh workflow run data-sync.yml" in runs and "-f trigger=watchdog" in runs
    assert "scan.py" not in runs and "sync_data.py" not in runs  # never bypasses the regular workflow


# ---------------------------------------------------------------- slots, gate, watchdog

from radar import slots  # noqa: E402

U = dt.timezone.utc


def run(key, status="success", event="schedule", started="2026-10-26T00:30:00+00:00"):
    return {"expected_4h_candle_utc": key, "status": status, "event": event, "started_utc": started}


def test_slot_keys_match_health_records():
    now = dt.datetime(2026, 10, 27, 23, 42, tzinfo=U)
    assert slots.last_close(now) == dt.datetime(2026, 10, 27, 20, tzinfo=U)
    assert slots.slot_key(slots.last_close(now)) == "2026-10-27T16:00+00:00"  # as written by health.py
    row, _ = health.build_row({"RUN_STARTED_UTC": now.isoformat(), "RUN_TRIGGER": "watchdog"}, now)
    assert row["expected_4h_candle_utc"] == "2026-10-27T16:00+00:00" and row["event"] == "watchdog"


def test_gate_skips_covered_slots_except_manual_runs():
    now = dt.datetime(2026, 10, 27, 23, 42, tzinfo=U)
    key = "2026-10-27T16:00+00:00"
    assert slots.gate([], now, "schedule")[0]
    assert not slots.gate([run(key)], now, "schedule")[0]            # watchdog already covered it
    assert not slots.gate([run(key)], now, "watchdog")[0]
    assert slots.gate([run(key, "failed")], now, "schedule")[0]      # a failed run does not cover a slot
    assert slots.gate([run(key)], now, "manual")[0]


def test_watchdog_decisions():
    key = "2026-10-27T16:00+00:00"
    early = dt.datetime(2026, 10, 27, 20, 30, tzinfo=U)
    late = dt.datetime(2026, 10, 27, 20, 47, tzinfo=U)
    assert slots.watchdog_decision([], [], early) == (False, f"waiting: slot {key} closed less than 40 min ago")
    assert slots.watchdog_decision([], [], late)[0]                                  # missed -> dispatch
    assert not slots.watchdog_decision([run(key)], [], late)[0]                      # covered
    assert not slots.watchdog_decision([], [{"status": "queued"}], late)[0]          # never two at once
    assert slots.watchdog_decision([run(key, "failed")], [], late)[0]                # one retry
    two = [run(key, "failed"), run(key, "partial")]
    ok, why = slots.watchdog_decision(two, [], late)
    assert not ok and "not retrying" in why                                           # bounded


def test_missed_slots_need_grace_and_any_run_counts():
    now = dt.datetime(2026, 10, 28, 4, 36, tzinfo=U)
    runs = [run("2026-10-27T12:00+00:00"), run("2026-10-27T16:00+00:00", "failed")]
    missed = slots.missed_slots(runs, now, hours=24)
    # closes in the last 24h: 04:00 (within grace), 00:00, 20:00 (failed run = attempted), 16:00 (run), 12:00, 08:00
    assert missed == ["2026-10-28T00:00+00:00", "2026-10-27T12:00+00:00", "2026-10-27T08:00+00:00"]


def test_slots_before_the_first_run_are_not_missed():
    now = dt.datetime(2026, 10, 28, 4, 36, tzinfo=U)
    runs = [run("2026-10-27T04:00+00:00", started="2026-10-27T08:53:00+00:00")]
    assert slots.missed_slots(runs, now) == ["2026-10-28T00:00+00:00", "2026-10-27T20:00+00:00",
                                            "2026-10-27T16:00+00:00", "2026-10-27T12:00+00:00"]
    assert slots.missed_slots([], now) == []
