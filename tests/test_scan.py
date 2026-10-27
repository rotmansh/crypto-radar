"""Scanner output rules: a WATCH carries no trade levels; the journal never duplicates a candle."""
import csv

import numpy as np

import scan
from radar.strategies import WATCH, BollingerSqueeze
from radar.strategies.base import fmt, fmt_pct
from test_strategies import b_frame, put


def watch_case():
    df = b_frame()
    put(df, "close", 102.0)  # inside the band: squeeze watch, no breakout
    put(df, "volume", 100.0)
    df["open_time"] = df.index.as_unit("ms").asi8
    df["close_time"] = df["open_time"] + 86_400_000 - 1
    df["atr_pct"], df["realized_vol_30"] = 2.0, 0.5
    return df


def test_watch_item_has_no_entry_stop_target_or_rr():
    s = BollingerSqueeze()
    df = watch_case()
    ev = s.evaluate(df)
    assert ev["state"].iloc[-1] == WATCH
    it = scan.item(s, "XUSDT", "1d", df, ev, len(df) - 1, {})
    assert it["stop"] is None and it["target"] is None and it["rr"] is None
    assert it["watch_invalidation"] == 100.0 and "מבטלת" in it["watch_invalidation_he"]
    assert it["breakout_level"] == 104.0


def _journal_row(df, at=-1):
    s = BollingerSqueeze()
    ev = s.evaluate(df)
    i = len(df) + at if at < 0 else at
    it = scan.item(s, "XUSDT", "1d", df, ev, i, {})
    from radar import engine
    return it, engine.episode_start(ev["state"], i), df


def test_journal_origin_and_no_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    import datetime as dt
    now = dt.datetime(2026, 10, 27, 7, tzinfo=dt.timezone.utc)
    df = watch_case()
    # Make the WATCH episode start 3 candles before the last one.
    for i in (-4, -3, -2):
        put(df, "close", 102.0, i)
        put(df, "bb_width_pct", 10.0, i)
    row = _journal_row(df)
    assert scan.append_journal([row], now, "abc", "binance") == {"live_already_active": 1}
    assert scan.append_journal([row], now, "abc", "binance") == {}  # same episode seen again: not re-added
    rows = list(csv.DictReader((tmp_path / "journal/signals-v2.csv").open()))
    assert len(rows) == 1
    r = rows[0]
    assert r["origin"] == "live_already_active" and r["stop"] == ""
    assert r["episode_started_utc"] == scan.iso(int(df["open_time"].iloc[-4]))   # computed from candles
    assert r["detected_at_utc"] == now.isoformat(timespec="seconds")             # real observation time
    assert r["episode_started_utc"] != r["candle_open_utc"]


def test_journal_live_new_and_v1_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    import datetime as dt
    import hashlib
    now = dt.datetime(2026, 10, 27, 7, tzinfo=dt.timezone.utc)
    df = watch_case()
    for i in (-4, -3, -2):
        put(df, "close", 99.9, i)  # below the middle band: no WATCH before the last candle
    it, ep, frame = _journal_row(df)
    assert scan.iso(int(frame["open_time"].iloc[ep])) == it["candle_open_utc"]
    # A v1 row of the same episode (v1 id formula) must prevent a duplicate in v2.
    v1_key = f"XUSDT|{it['strategy']}@{it['strategy_version']}|1d|WATCH|{it['candle_open_utc']}"
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal/signals.csv").write_text("signal_id\n" + hashlib.sha256(v1_key.encode()).hexdigest()[:16] + "\n")
    assert scan.append_journal([(it, ep, frame)], now, "abc", "binance") == {}
    (tmp_path / "journal/signals.csv").unlink()
    assert scan.append_journal([(it, ep, frame)], now, "abc", "binance") == {"live_new": 1}


def test_number_formats_are_readable():
    assert fmt(80150.56) == "80,150.6"
    assert fmt(0.00000432) == "0.00000432"
    assert fmt(float("nan")) == "—"
    assert fmt_pct(0.301541) == "0.30"


def test_every_strategy_defines_watch_invalidation():
    from radar.strategies import ALL
    for s in ALL:
        assert type(s).watch_invalidation is not type(s).__mro__[1].watch_invalidation
