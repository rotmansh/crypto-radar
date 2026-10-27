"""Rule-by-rule tests for the three strategies, including the cases where NO signal may be emitted.

Frames here are hand-built TEST FIXTURES with indicator columns set directly, so each rule can be switched on
or off in isolation. The no-lookahead tests at the bottom also run on the real stored Binance data.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from conftest import DAY, make_candles

from radar import indicators as ind
from radar.store import CandleStore
from radar.strategies import ALL, INSUFFICIENT, NONE, SIGNAL, WATCH, BollingerSqueeze, SMA50Pullback, \
    TrendRelativeStrength, rs_panel

ROOT = Path(__file__).resolve().parent.parent
A, B, C = SMA50Pullback(), BollingerSqueeze(), TrendRelativeStrength()


def frame(n):
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(index=idx, data={"partial": False, "volume": 100.0})


def state(strategy, df, ctx=None, at=-1):
    return strategy.evaluate(df, ctx).iloc[at]


# ---------------------------------------------------------------- A: SMA 50 pullback

def a_frame(n=230):
    """Every A rule passes on the last candle."""
    df = frame(n)
    df["sma200"] = np.linspace(80, 90, n)
    df["sma50"] = np.linspace(95, 100, n)
    df["atr14"] = 2.0
    df["close"] = df["sma50"] + 5
    df["open"] = df["close"] - 0.5
    df["high"] = df["close"] + 1
    df["low"] = df["close"] - 1
    t = n - 1
    for i in (t - 100, t - 50):  # two respected touches inside [t-160, t-11]
        df.iloc[i, df.columns.get_loc("low")] = df["sma50"].iloc[i]
    s = df["sma50"]
    set_ = lambda i, **kw: [df.__setitem__(k, df[k].where(df.index != df.index[i], v)) for k, v in kw.items()]
    set_(t - 2, low=s.iloc[t - 2] * 1.005, close=s.iloc[t - 2] * 1.002, open=s.iloc[t - 2] * 1.01,
         high=s.iloc[t - 2] * 1.012)
    set_(t - 1, low=s.iloc[t - 1] * 1.001, close=s.iloc[t - 1] + 0.5, open=s.iloc[t - 1] + 0.3,
         high=s.iloc[t - 1] + 1)
    set_(t, open=s.iloc[t] + 0.2, close=s.iloc[t] + 1.5, high=s.iloc[t] + 1.7, low=s.iloc[t] + 0.1)
    return df


def put(df, col, value, at=-1):
    df.iloc[at, df.columns.get_loc(col)] = value
    return df


def test_a_signal_when_all_rules_hold():
    r = state(A, a_frame())
    assert r.state == SIGNAL
    assert r.respect_count == 2
    assert r.stop == pytest.approx(min(a_frame()["low"].iloc[-5:]) - 0.4)
    assert "כיבודים" in A.reason_he(r)


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: put(d, "sma200", 200.0), NONE),                                          # close below SMA200
    (lambda d: d.assign(sma200=np.linspace(80, 99.9, len(d)) + d["sma50"] * 0 + 1), None),  # SMA50 not above SMA200 at t
    (lambda d: d.assign(sma200=np.linspace(90, 80, len(d))), NONE),                     # SMA200 falling
    (lambda d: put(d, "sma50", d["sma50"].iloc[-11] - 0.1), None),                      # SMA50 lower than 10 candles ago
])
def test_a_trend_rules_block_signal(mutate, expected):
    r = state(A, mutate(a_frame()))
    assert r.state != SIGNAL
    if expected:
        assert r.state == expected


def test_a_needs_two_prior_respects():
    df = a_frame()
    df.iloc[-101, df.columns.get_loc("low")] = df["close"].iloc[-101] - 1  # remove one touch
    r = state(A, df)
    assert r.respect_count == 1 and r.c_prior_respects == 0 and r.state == NONE


def test_a_failed_touch_is_not_a_respect():
    df = a_frame()
    i = len(df) - 101
    # after the touch, price closes below SMA50×0.97 before reaching SMA50+ATR
    for j in range(i + 1, i + 11):
        df.iloc[j, df.columns.get_loc("close")] = df["sma50"].iloc[j] * 0.96
        df.iloc[j, df.columns.get_loc("high")] = df["sma50"].iloc[j] * 0.99
        df.iloc[j, df.columns.get_loc("low")] = df["sma50"].iloc[j] * 0.95
    r = state(A, df)
    assert r.respect_count == 1 and r.state != SIGNAL


def test_a_touches_close_together_count_once():
    df = a_frame()
    i = len(df) - 101
    df.iloc[i + 3, df.columns.get_loc("low")] = df["sma50"].iloc[i + 3]  # second touch 3 candles later
    assert state(A, df).respect_count == 2


def test_a_requires_prior_extension():
    df = a_frame()
    for j in range(len(df) - 21, len(df) - 1):
        df.iloc[j, df.columns.get_loc("close")] = df["sma50"].iloc[j] + 0.5  # never 1 ATR above
    r = state(A, df)
    assert r.c_extension == 0 and r.state == NONE


def test_a_requires_touch_in_last_5():
    df = a_frame()
    for j in range(len(df) - 5, len(df)):
        df.iloc[j, df.columns.get_loc("low")] = df["sma50"].iloc[j] * 1.02
    r = state(A, df)
    assert r.c_touch_recent == 0 and r.state == NONE


def test_a_close_below_098_blocks():
    df = a_frame()
    put(df, "close", df["sma50"].iloc[-3] * 0.979, -3)
    r = state(A, df)
    assert r.c_no_close_break == 0 and r.state == NONE


@pytest.mark.parametrize("col,value,rule", [
    ("close", None, "green"),                 # red candle
    ("high", None, "close_above_prev_high"),  # does not clear previous high
])
def test_a_without_confirmation_is_watch(col, value, rule):
    df = a_frame()
    if rule == "green":
        put(df, "open", df["close"].iloc[-1] + 0.1)
        put(df, "high", df["close"].iloc[-1] + 0.2)
    else:
        put(df, "high", df["close"].iloc[-1] + 1, -2)
    r = state(A, df)
    assert r[f"c_{rule}"] == 0 and r.state == WATCH
    assert "חסר לאישור" in A.reason_he(r)


def test_a_partial_candle_never_confirms():
    r = state(A, put(a_frame(), "partial", True))
    assert r.c_complete == 0 and r.state == WATCH


def test_a_short_history_is_insufficient():
    df = a_frame()
    df["sma200"] = np.nan
    assert state(A, df).state == INSUFFICIENT


def test_a_cooldown_suppresses_repeat_signal():
    df = a_frame()  # SIGNAL on the last candle
    prev_close = df["close"].iloc[-1]
    nxt = df.iloc[[-1]].copy()  # next candle confirms again (green, above the previous high)
    nxt.index = nxt.index + pd.Timedelta(days=1)
    nxt["open"], nxt["close"], nxt["high"], nxt["low"] = prev_close, prev_close + 1.5, prev_close + 1.7, prev_close - 0.1
    df2 = pd.concat([df, nxt])
    ev = A.evaluate(df2)
    assert ev["state"].iloc[-2] == SIGNAL
    assert ev["state"].iloc[-1] == NONE and bool(ev["suppressed_by_cooldown"].iloc[-1])


def test_a_rr_not_computable_when_target_below_entry():
    df = a_frame()
    df["high"] = df["high"].where(df.index >= df.index[-5], df["close"].iloc[-1] - 0.5)
    r = state(A, df)
    assert r.state == SIGNAL and np.isnan(r.rr)


# ---------------------------------------------------------------- B: Bollinger squeeze

def b_frame(n=210):
    df = frame(n)
    df["bb_mid"], df["bb_upper"], df["bb_lower"] = 100.0, 104.0, 96.0
    df["bb_width_pct"] = 50.0
    df["close"], df["open"], df["high"], df["low"] = 100.5, 100.2, 101.0, 99.5
    df["vol_sma20_prev"] = 100.0
    for i in (-4, -3, -2):
        put(df, "bb_width_pct", 10.0, i)
    put(df, "close", 106.0)
    put(df, "high", 106.5)
    put(df, "low", 105.5)
    put(df, "volume", 200.0)
    return df


def test_b_breakout_signal_and_levels():
    r = state(B, b_frame())
    assert r.state == SIGNAL
    assert r.stop == 100.0  # min(SMA20, low)
    assert r.target == pytest.approx(106 + (101 - 99.5))
    assert r.rr == pytest.approx(1.5 / 6)


def test_b_no_squeeze_no_signal():
    df = b_frame()
    df["bb_width_pct"] = 50.0
    assert state(B, df).state == NONE


def test_b_squeeze_must_precede_breakout_candle():
    df = b_frame()
    df["bb_width_pct"] = 50.0
    put(df, "bb_width_pct", 10.0)  # squeeze only on the breakout candle itself
    r = state(B, df)
    assert r.c_squeeze_prior == 0 and r.state != SIGNAL


def test_b_must_clear_range_high():
    df = put(b_frame(), "high", 110.0, -10)
    r = state(B, df)
    assert r.c_close_above_range == 0 and r.state == WATCH  # still below breakout level 110


def test_b_needs_volume():
    r = state(B, put(b_frame(), "volume", 149.0))
    assert r.c_volume_surge == 0 and r.state != SIGNAL


def test_b_close_inside_band_is_not_breakout():
    r = state(B, put(b_frame(), "close", 103.9))
    assert r.c_close_above_upper == 0 and r.state == WATCH


def test_b_watch_requires_upper_half():
    df = b_frame()
    put(df, "close", 99.0)
    put(df, "volume", 100.0)
    assert state(B, df).state == NONE


def test_b_partial_candle_never_confirms():
    assert state(B, put(b_frame(), "partial", True)).state != SIGNAL


def test_b_short_history_is_insufficient():
    df = b_frame()
    df["bb_width_pct"] = np.nan
    assert state(B, df).state == INSUFFICIENT


# ---------------------------------------------------------------- C: trend & relative strength

def c_case(n=60):
    df = frame(n)
    df["sma200"], df["atr14"] = 90.0, 2.0
    df["sma50"] = np.linspace(95, 100, n)
    df["close"] = 105.0
    df["open"], df["high"], df["low"] = 104.0, 105.5, 103.5
    for i in range(n - 21, n - 1):
        put(df, "close", 107.0, i)  # 20-candle closing high = 107; last new high was 20 candles ago
    put(df, "close", 110.0)
    rs = pd.DataFrame(index=df.index, data={"rs": 1.2, "rs_sma50": 1.0, "rs_ret30": 0.10, "rs_rank": 90.0,
                                            "rs_universe_n": 40.0})
    return df, {"rs": rs}


def test_c_signal_on_first_new_high_with_strength():
    df, ctx = c_case()
    r = state(C, df, ctx)
    assert r.state == SIGNAL and r.c_transition == 1
    assert r.stop == pytest.approx(110 - 2.5 * 2) and np.isnan(r.target) and np.isnan(r.rr)


@pytest.mark.parametrize("col,value", [("rs_rank", 79.0), ("rs_ret30", -0.01), ("rs", 0.99)])
def test_c_relative_strength_rules_block(col, value):
    df, ctx = c_case()
    ctx["rs"].iloc[-1, ctx["rs"].columns.get_loc(col)] = value
    assert state(C, df, ctx).state == NONE


def test_c_trend_rules_block():
    df, ctx = c_case()
    put(df, "sma200", 101.0)  # SMA50 below SMA200
    assert state(C, df, ctx).state == NONE
    df, ctx = c_case()
    put(df, "sma50", 90.0)  # SMA50 lower than 10 candles ago
    assert state(C, df, ctx).state != SIGNAL


def test_c_no_repeat_while_conditions_persist():
    df, ctx = c_case()
    put(df, "close", 109.0, -2)  # yesterday already a new high with everything in place
    r = state(C, df, ctx)
    assert r.c_transition == 0 and r.state == NONE


def test_c_consolidating_leader_is_watch():
    df, ctx = c_case()
    put(df, "close", 106.5)
    r = state(C, df, ctx)
    assert r.state == WATCH and r.c_no_new_high == 1


def test_c_missing_cross_section_is_insufficient():
    df, ctx = c_case()
    ctx["rs"]["rs_rank"] = np.nan
    assert state(C, df, ctx).state == INSUFFICIENT


def test_c_partial_candle_never_confirms():
    df, ctx = c_case()
    assert state(C, put(df, "partial", True), ctx).state != SIGNAL


def test_rs_panel_ranks_and_requires_cross_section():
    btc = ind.to_frame(make_candles(80, DAY, 1_704_067_200_000, seed=99))
    coins = {f"C{i}": ind.to_frame(make_candles(80, DAY, 1_704_067_200_000, seed=i)) for i in range(12)}
    panel = rs_panel(coins, btc)
    last = {s: d["rs_ret30"].iloc[-1] for s, d in panel.items()}
    top = max(last, key=last.get)
    assert panel[top]["rs_rank"].iloc[-1] == 100.0
    assert panel[top]["rs_universe_n"].iloc[-1] == 12
    small = rs_panel({k: coins[k] for k in list(coins)[:5]}, btc)
    assert small["C0"]["rs_rank"].isna().all()  # fewer than 10 coins: rank undefined, not guessed


def test_rs_does_not_forward_fill_missing_btc():
    btc = ind.to_frame(make_candles(80, DAY, 1_704_067_200_000, seed=99))
    coin = ind.to_frame(make_candles(80, DAY, 1_704_067_200_000, seed=1))
    p = rs_panel({"X": coin}, btc.drop(btc.index[40]))
    assert np.isnan(p["X"]["rs"].iloc[40]) and not np.isnan(p["X"]["rs"].iloc[41])


# ---------------------------------------------------------------- versions and lookahead

EXPECTED_PARAM_HASHES = {
    "A-SMA50PB@1.0.0": "ff7efc6d7c94",
    "B-BBSQ@1.0.0": "9bbcd16acf0c",
    "C-TRS@1.1.0": "5ccc403477ab",
}


def test_params_are_pinned_to_versions():
    """Changing a parameter without bumping the version must fail here (SPEC §3.0)."""
    assert {s.ref: s.params_hash for s in ALL} == EXPECTED_PARAM_HASHES


def _check_no_lookahead(strategy, df, ctx_fn, cut_points, interval):
    full = strategy.evaluate(df, ctx_fn(df))
    for t in cut_points:
        part_df = df.iloc[: t + 1]
        part = strategy.evaluate(part_df, ctx_fn(part_df))
        cols = ["state", "stop", "target"] + [c for c in full.columns if c.startswith("c_")]
        pd.testing.assert_frame_equal(full[cols].iloc[: t + 1], part[cols], check_exact=False, rtol=0, atol=1e-9)


@pytest.mark.parametrize("strategy", [A, B])
def test_no_lookahead_synthetic(strategy):
    df = ind.compute_all(ind.to_frame(make_candles(500, DAY, 1_704_067_200_000, seed=11)))
    _check_no_lookahead(strategy, df, lambda d: {}, (240, 300, 377, 450, 499), "1d")


def test_no_lookahead_c_synthetic():
    btc = ind.compute_all(ind.to_frame(make_candles(400, DAY, 1_704_067_200_000, seed=99)))
    coins = {f"C{i}": ind.compute_all(ind.to_frame(make_candles(400, DAY, 1_704_067_200_000, seed=i)))
             for i in range(12)}
    full_panel = rs_panel(coins, btc)
    full = C.evaluate(coins["C3"], {"rs": full_panel["C3"]})
    for t in (210, 260, 333, 399):
        cut = {k: v.iloc[: t + 1] for k, v in coins.items()}
        part_panel = rs_panel(cut, btc.iloc[: t + 1])
        part = C.evaluate(cut["C3"], {"rs": part_panel["C3"]})
        assert full["state"].iloc[: t + 1].tolist() == part["state"].tolist()


STORE = CandleStore(ROOT / "data/ohlcv", "binance")


@pytest.mark.skipif(not (ROOT / "data/ohlcv/binance/4h/ETHUSDT").exists(), reason="real data not present")
@pytest.mark.parametrize("interval", ["1d", "4h"])
@pytest.mark.parametrize("strategy", [A, B])
def test_no_lookahead_real_data(strategy, interval):
    """Real Binance ETHUSDT candles: truncating after t never changes a decision at or before t."""
    candles = STORE.load("ETHUSDT", interval)[-900:]
    df = ind.compute_all(ind.to_frame(candles), interval)
    n = len(df)
    _check_no_lookahead(strategy, df, lambda d: {}, (n - 400, n - 251, n - 97, n - 2), interval)


# ---------------------------------------------------------------- research variant & high-risk flag

def test_research_tail_variant_is_separate_and_pinned():
    from radar.strategies import RESEARCH
    assert {s.ref: s.params_hash for s in RESEARCH} == {
        "A-SMA50PB-TAIL@0.1.0": "a92eead115c1", "A-SMA50PB-TAIL@0.2.0": "c6007cffcf27",
        "A-SMA50PB-TAIL@0.3.0": "9d184b67c834"}
    assert not {s.id for s in RESEARCH} & {s.id for s in ALL}  # never part of the live scan
    assert A.ref == "A-SMA50PB@1.0.0" and "max_tail_atr" not in A.params  # base version untouched


def test_research_tail_limits_wick_depth_base_does_not():
    from radar.strategies import SMA50PullbackTail
    df = a_frame()
    put(df, "low", df["sma50"].iloc[-3] - 1.5 * 2.0, -3)  # wick 1.5 ATR below SMA50, close unchanged
    assert state(A, df).state == SIGNAL                    # base rule allows any wick depth
    r1 = state(SMA50PullbackTail("0.2.0"), df)             # k = 1.0
    assert r1.c_tail_within_atr == 0 and r1.state == NONE
    assert state(SMA50PullbackTail("0.3.0"), df).state == SIGNAL  # k = 1.5


def test_high_risk_flag_marks_but_never_moves_the_stop():
    from radar.present import item
    df, ctx = c_case()
    df["atr14"] = 6.0  # stop = 110 - 2.5*6 = 95  -> 13.6% away
    df["open_time"] = df.index.as_unit("ms").asi8
    df["close_time"] = df["open_time"] + 86_400_000 - 1
    df["atr_pct"], df["realized_vol_30"] = 5.0, 1.0
    ev = C.evaluate(df, ctx)
    it = item(C, "XUSDT", "1d", df, ev, len(df) - 1, {}, 10.0)
    assert it["state"] == SIGNAL and it["high_risk"] is True
    assert it["stop"] == pytest.approx(95.0)
    assert not item(C, "XUSDT", "1d", df, ev, len(df) - 1, {}, 20.0)["high_risk"]
