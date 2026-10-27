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
    df["open_time"] = (df.index.astype("int64") // 10**6).astype("int64")
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


def test_journal_never_duplicates_a_candle(tmp_path, monkeypatch):
    monkeypatch.setattr(scan, "ROOT", tmp_path)
    s = BollingerSqueeze()
    df = watch_case()
    ev = s.evaluate(df)
    row = (s, "XUSDT", "1d", df, ev, len(df) - 1, {})
    import datetime as dt
    now = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
    assert scan.append_journal([row], now, "abc", "binance") == 1
    assert scan.append_journal([row], now, "abc", "binance") == 0  # a later run sees the same candle
    rows = list(csv.DictReader((tmp_path / "journal/signals.csv").open()))
    assert len(rows) == 1 and rows[0]["type"] == WATCH and rows[0]["stop"] == ""


def test_number_formats_are_readable():
    assert fmt(80150.56) == "80,150.6"
    assert fmt(0.00000432) == "0.00000432"
    assert fmt(float("nan")) == "—"
    assert fmt_pct(0.301541) == "0.30"


def test_every_strategy_defines_watch_invalidation():
    from radar.strategies import ALL
    for s in ALL:
        assert type(s).watch_invalidation is not type(s).__mro__[1].watch_invalidation
