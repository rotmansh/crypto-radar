"""Strategy D (research): pinned versions, higher-timeframe alignment without lookahead, entry rules causal,
trade management (break-even, trailing, 72h review, trend exit). docs/STRATEGY_D_SPEC.md."""
import numpy as np
import pandas as pd
import pytest

from radar import backtest as bt
from radar import indicators
from radar.strategies import ALL, RESEARCH, SWING_RESEARCH
from radar.strategies.sma50_swing import SMA50Swing, asof

H1, H4, DAY = 3_600_000, 14_400_000, 86_400_000
T0 = 1_600_000_000_000 // DAY * DAY


def test_versions_are_pinned_and_separate():
    assert {s.ref: s.params_hash for s in SWING_RESEARCH} == {
        "D-SMA50SWING@0.1.0": "4a65232315bc", "D-SMA50SWING@0.2.0": "ed0a05721ef4",
        "D-SMA50SWING@0.3.0": "27ed934a446c", "D-SMA50SWING@0.4.0": "067f710ccd7b",
        "D-SMA50SWING@0.5.0": "a315ca4769ce", "D-SMA50SWING@0.6.0": "4001ed2db0c3"}
    ids = {s.id for s in SWING_RESEARCH}
    assert not ids & {s.id for s in ALL} and not ids & {s.id for s in RESEARCH}   # not in the live scan
    entry = {k: v for k, v in SWING_RESEARCH[0].params.items() if k not in ("break_even_after_1r", "trail_atr")}
    assert all({k: v for k, v in s.params.items() if k not in ("break_even_after_1r", "trail_atr")} == entry
               for s in SWING_RESEARCH)                                          # identical entry rules


def candles(n, span, seed, drift=0.0008, vol=0.01, start=T0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    ot = start + np.arange(n, dtype=np.int64) * span
    df = pd.DataFrame({"open_time": ot, "close_time": ot + span - 1, "open": open_, "high": high, "low": low,
                       "close": close, "volume": 1e6, "quote_volume": 30e6 * (1 + rng.uniform(0, 1, n))})
    return df.set_index(pd.to_datetime(ot, unit="ms", utc=True), drop=False)


def test_asof_never_uses_a_higher_candle_that_closed_later():
    f = candles(12, H1, 1)
    htf = candles(3, H4, 2)
    v = asof(f, htf, htf["close"])
    # 1h candle 0..2 close before the first 4h candle closes (03:59:59.999) -> no value; candle 3 closes with it
    assert np.isnan(v[:3]).all() and v[3] == htf["close"].iloc[0] and v[7] == htf["close"].iloc[1]


def build(tf, n=None, seed=3):
    """A coin that pulls back inside a rising higher-timeframe trend: produces WATCH and SIGNAL states."""
    n = n or {"1h": 3000, "4h": 2400}[tf]
    span = {"1h": H1, "4h": H4}[tf]
    f = indicators.compute_all(candles(n, span, seed, drift=0.0006, vol=0.012), tf)
    hspan = {"1h": H4, "4h": DAY}[tf]
    htf = indicators.compute_all(candles(n * span // hspan + 2, hspan, seed + 1, drift=0.004, vol=0.01),
                                 "4h" if tf == "1h" else "1d")
    daily = indicators.compute_all(candles(n * span // DAY + 2, DAY, seed + 2), "1d")
    series = {"1d": daily, tf: f}
    series["4h" if tf == "1h" else "1d"] = htf
    return f, series


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_entry_rules_use_no_future_data(tf):
    s = SMA50Swing("0.2.0")
    f, series = build(tf)
    full = s.evaluate(f, {"series": series, "timeframe": tf})
    assert (full["state"] == "SIGNAL").any() and (full["state"] == "WATCH").any()
    sig = list(np.flatnonzero(full["state"].to_numpy() == "SIGNAL"))
    for cut in sig[:3] + [len(f) // 2, len(f) - 1]:
        part_f = f.iloc[: cut + 1]
        close = int(part_f["close_time"].iloc[-1])
        part_series = {k: v[v["close_time"] <= close] for k, v in series.items()}
        part_series[tf] = part_f
        part = s.evaluate(part_f, {"series": part_series, "timeframe": tf})
        cols = ["state", "stop"] + [c for c in full.columns if c.startswith("c_")]
        pd.testing.assert_frame_equal(full[cols].iloc[: cut + 1], part[cols], check_exact=False, rtol=0, atol=1e-9)


def test_signal_requires_watch_before_and_cost_rule():
    s = SMA50Swing("0.1.0")
    f, series = build("4h")
    ev = s.evaluate(f, {"series": series, "timeframe": "4h"})
    sig = ev[ev["state"] == "SIGNAL"]
    assert len(sig) >= 3
    assert (sig["c_watch_before"] == 1).all() and (sig["c_risk_vs_cost"] == 1).all()
    assert (sig["c_stop_not_too_wide"] == 1).all()
    k = ev["risk_cost_ratio"].last_valid_index()
    row = ev.loc[k]
    slip = bt.slippage(row["median_qvol"])
    assert row["risk_cost_ratio"] == pytest.approx((row["close"] - row["stop"]) / (2 * (bt.FEE + slip) * row["close"]))


# ---------------------------------------------------------------- management

def frame(closes, lows=None, highs=None, sma50=90.0, atr=1.0, tf="4h"):
    span = H4 if tf == "4h" else H1
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    lows = np.minimum(opens, closes) - 0.1 if lows is None else np.asarray(lows, dtype=float)
    highs = np.maximum(opens, closes) + 0.1 if highs is None else np.asarray(highs, dtype=float)
    ot = T0 + np.arange(n, dtype=np.int64) * span
    df = pd.DataFrame({"open_time": ot, "close_time": ot + span - 1, "open": opens, "high": highs, "low": lows,
                       "close": closes, "partial": False, "sma50": sma50, "atr14": atr})
    return df.set_index(pd.to_datetime(ot, unit="ms", utc=True), drop=False)


def ev_for(f, stop, htf=True):
    ev = pd.DataFrame(index=f.index)
    ev["stop"] = np.nan
    ev.iloc[0, 0] = stop
    ev["c_htf_trend"] = 1.0 if htf else 0.0
    ev["c_htf_rising"] = 1.0
    return ev


def run(version, f, ev, tf="4h"):
    s = SMA50Swing(version)
    return bt.simulate_d(tf, f, ev, 0, 1e9, "S", "XUSDT", True, s.params)


def test_break_even_after_one_r_then_exit_at_cost_neutral_level():
    # entry 100 (open of candle 1), stop 95 -> 1R = 5; close 105.5 >= 105 arms break-even; then a drop
    f = frame([100, 100, 103, 105.5, 104, 99, 98], sma50=80.0)
    t, _ = run("0.1.0", f, ev_for(f, 95))
    assert t.exit_kind == "breakeven" and t.exit_raw == pytest.approx(bt.break_even_level(100, t.slip))
    assert t.r[1.0] == pytest.approx(0.0, abs=1e-9)
    t4, _ = run("0.4.0", f, ev_for(f, 95))                     # same path without break-even keeps the trade
    assert t4.exit_kind != "breakeven"


def test_trailing_only_starts_at_entry_without_break_even():
    # no break-even, m=2: stop = max(stop, max close - 2*ATR) from the first close
    f = frame([100, 100, 101, 102, 99.5, 99], sma50=80.0)
    t, _ = run("0.5.0", f, ev_for(f, 95))
    assert t.exit_kind == "trail" and t.exit_raw == pytest.approx(100.0)   # 102 - 2*1
    t2, _ = run("0.2.0", f, ev_for(f, 95))                                 # with break-even: trail waits for 1R
    assert t2.exit_kind != "trail"


def test_review_after_72_hours_exits_if_trade_did_not_progress():
    closes = [100] * 25                                                    # flat: never reaches +0.5R
    f = frame(closes, sma50=99.0)
    t, _ = run("0.4.0", f, ev_for(f, 95))
    assert t.exit_kind == "review" and t.exit_i == 1 + 18 and t.exit_phase == 0   # at the open after 18 candles


def test_review_keeps_a_trade_whose_trend_continues():
    closes = [100] + list(np.linspace(100, 110, 30))
    sma = np.linspace(90, 99, 31)
    f = frame(closes, sma50=sma)
    t, _ = run("0.4.0", f, ev_for(f, 95))
    assert t.exit_kind == "period_end" and t.bars_held > 18                 # held past 72 hours
    t_bad, _ = run("0.4.0", f, ev_for(f, 95, htf=False))                   # higher-timeframe trend gone
    assert t_bad.exit_kind == "review"


def test_trend_exit_after_two_closes_below_sma50():
    f = frame([100, 100, 101, 89.5, 89.4, 89, 88], sma50=90.0, atr=1.0, lows=[99, 99, 100, 89, 89, 88.5, 87.5])
    t, _ = run("0.4.0", f, ev_for(f, 85))
    assert t.exit_kind == "trend" and t.exit_i == 5


def test_review_window_is_72_hours_on_1h():
    f = frame([100] * 80, sma50=99.0, tf="1h")
    t, _ = run("0.4.0", f, ev_for(f, 95), tf="1h")
    assert t.exit_kind == "review" and t.exit_i == 1 + 72
