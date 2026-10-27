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
    assert int(minute) > 0 and sorted(int(h) for h in hours.split(",")) == [0, 4, 8, 12, 16, 20]
    assert "workflow_dispatch" in on
    assert w["concurrency"]["cancel-in-progress"] is False
    steps = {s.get("id"): s for s in w["jobs"]["sync"]["steps"] if s.get("id")}
    assert steps["health"]["if"] == "always()"
    assert "always()" in w["jobs"]["site"]["if"]  # the dashboard is republished to show a failure
    assert w["jobs"]["verify"]["needs"] == ["sync", "site"]
