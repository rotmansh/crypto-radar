"""Phase 5 backtest rules (docs/PHASE5_PLAN.md): fills, gaps, R, C exits without lookahead, portfolio cash,
benchmarks and metrics."""
import math

import numpy as np
import pandas as pd
import pytest

from radar import backtest as bt

H4 = 14_400_000
DAY = 86_400_000
T0 = 1_600_000_000_000 // DAY * DAY


def frame(rows, tf="1d", sma50=None, atr=None):
    """rows: (open, high, low, close)."""
    span = DAY if tf == "1d" else H4
    n = len(rows)
    ot = np.array([T0 + k * span for k in range(n)], dtype=np.int64)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype=float)
    df["open_time"], df["close_time"] = ot, ot + span - 1
    df["partial"] = False
    df["sma50"] = sma50 if sma50 is not None else 1.0
    df["atr14"] = atr if atr is not None else 1.0
    return df.set_index(pd.to_datetime(ot, unit="ms", utc=True), drop=False)


def ev_for(f, i, stop, range_high=np.nan, range_low=np.nan):
    ev = pd.DataFrame(index=f.index)
    ev["state"] = "NONE"
    ev.iloc[i, ev.columns.get_loc("state")] = "SIGNAL"
    ev["stop"] = np.nan
    ev.iloc[i, ev.columns.get_loc("stop")] = stop
    ev["range_high"], ev["range_low"] = range_high, range_low
    return ev


def run(kind, f, ev, i=0, tf="1d", med=1e9, end=True):
    return bt.simulate(kind, tf, f, ev, i, med, "S", "XUSDT", end)


def test_slippage_tiers():
    assert bt.slippage(600e6) == 0.0002 and bt.slippage(100e6) == 0.0005
    assert bt.slippage(20e6) == 0.001 and bt.slippage(1e6) == 0.002 and bt.slippage(np.nan) == 0.002


def test_normal_stop_is_exactly_minus_one_r_and_costs_are_in_the_denominator():
    f = frame([(100, 101, 99, 100), (100, 100.5, 94, 95)] + [(95, 96, 94, 95)] * 3)
    t, why = run("A", f, ev_for(f, 0, stop=95))
    assert why is None and t.exit_kind == "stop" and t.exit_raw == 95
    assert t.r[1.0] == pytest.approx(-1.0)                      # planned risk includes fees + slippage
    s = bt.slippage(1e9)
    assert t.r_unit == pytest.approx((100 * (1 + s) - 95 * (1 - s)) + bt.FEE * (100 * (1 + s) + 95 * (1 - s)))
    assert -1 < t.r[0.0] < 0 and t.r[2.0] < -1                   # same trade, same denominator, other costs


def test_gap_below_stop_at_entry_is_not_entered():
    f = frame([(100, 101, 99, 100), (94, 95, 90, 91)])
    assert run("A", f, ev_for(f, 0, stop=95)) == (None, "gap_below_stop")


def test_gap_through_the_stop_loses_more_than_one_r():
    f = frame([(100, 101, 99, 100), (100, 101, 98, 99), (80, 82, 79, 81)])
    t, _ = run("A", f, ev_for(f, 0, stop=95))
    assert t.exit_kind == "stop_gap" and t.exit_raw == 80 and t.r[1.0] < -3


def test_stop_first_when_stop_and_target_in_same_candle():
    f = frame([(100, 101, 99, 100), (100, 115, 94, 100)])
    t, _ = run("A", f, ev_for(f, 0, stop=95))
    assert t.exit_kind == "stop" and t.same_candle_stop_target


def test_a_target_is_two_r_from_the_entry_open_with_slippage():
    f = frame([(100, 101, 99, 100), (100, 105, 98, 104), (104, 111, 103, 110)])
    t, _ = run("A", f, ev_for(f, 0, stop=95))
    assert t.target == 110 and t.exit_kind == "target" and t.exit_raw == 110
    entry, exit_, _ = t.eff(1.0)
    assert exit_ == pytest.approx(110 * (1 - t.slip))                  # target fill also pays slippage
    assert t.r[1.0] < 2


def test_b_target_and_time_exit():
    rows = [(100, 101, 99, 100)] + [(100, 101, 99, 100)] * 25
    f = frame(rows)
    t, _ = run("B", f, ev_for(f, 0, stop=95, range_high=110, range_low=90))
    assert t.target == 120 and t.exit_kind == "time" and t.exit_i == 1 + 20 and t.bars_held == 20
    f4 = frame(rows + [(100, 101, 99, 100)] * 10, tf="4h")
    t4, _ = run("B", f4, ev_for(f4, 0, stop=95, range_high=110, range_low=90), tf="4h")
    assert t4.exit_i == 1 + 30


def test_c_exits_on_trailing_stop_at_next_open():
    closes = [100, 101, 110, 120, 116, 115]   # trail = max close - 3*ATR(=1) -> 117 after 120; 116 < 117
    rows = [(c, c + 0.5, c - 0.5, c) for c in closes] + [(113, 114, 112, 113)]
    f = frame(rows, sma50=50.0)
    t, _ = run("C", f, ev_for(f, 0, stop=90))
    assert t.exit_kind == "trail" and t.exit_i == 5 and t.exit_raw == 115 and t.exit_phase == 0


def test_c_exits_below_sma50_and_hard_stop():
    rows = [(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 97, 98), (97, 98, 96, 97)]
    f = frame(rows, sma50=99.0, atr=10.0)
    t, _ = run("C", f, ev_for(f, 0, stop=80))
    assert t.exit_kind == "sma50" and t.exit_i == 3
    rows = [(100, 101, 99, 100), (100, 101, 79, 85)]
    f = frame(rows, sma50=50.0, atr=10.0)
    t, _ = run("C", f, ev_for(f, 0, stop=80))
    assert t.exit_kind == "stop" and t.exit_raw == 80


def test_c_exit_uses_no_future_data():
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0.3, 2, 300))
    rows = [(c, c + 1, c - 1, c) for c in closes]
    sma = pd.Series(closes).rolling(50, min_periods=1).mean().to_numpy()
    f = frame(rows, sma50=sma, atr=2.0)
    ev = ev_for(f, 60, stop=closes[60] - 5)
    t, _ = run("C", f, ev, i=60)
    cut = f.iloc[: t.exit_i + 1]
    t2, _ = run("C", cut, ev.iloc[: t.exit_i + 1], i=60)
    assert (t2.exit_i, t2.exit_kind, t2.exit_raw) == (t.exit_i, t.exit_kind, t.exit_raw)
    wild = f.copy()
    wild.iloc[t.exit_i + 1:, wild.columns.get_loc("close")] *= 10   # change everything after the exit
    t3, _ = run("C", wild, ev, i=60)
    assert (t3.exit_i, t3.exit_kind) == (t.exit_i, t.exit_kind)


def test_open_trade_at_data_end_is_closed_and_labelled():
    f = frame([(100, 101, 99, 100)] * 5)
    t, _ = run("C", f, ev_for(f, 0, stop=90), end=False)
    assert t.exit_kind == "data_end" and t.exit_raw == 100
    t, _ = run("C", f, ev_for(f, 0, stop=90), end=True)
    assert t.exit_kind == "period_end"


def make_trade(sym, entry_i, exit_i, entry, exit_, stop, kind="stop", med=1e9, phase=2):
    t = bt.Trade("S", sym, entry_i - 1, T0 + (entry_i - 1) * DAY, entry_i, T0 + entry_i * DAY, entry, stop,
                 math.nan, bt.slippage(med), med, bt.planned_risk(entry, stop, bt.slippage(med)))
    t.exit_i, t.exit_time, t.exit_raw, t.exit_kind, t.exit_phase = exit_i, T0 + exit_i * DAY, exit_, kind, phase
    return bt.apply_costs(t)


def flat_frames(syms, n=40, px=100.0):
    return {s: frame([(px, px, px, px)] * n) for s in syms}


def test_portfolio_risk_sizing_cash_and_max_positions():
    syms = [f"C{k}USDT" for k in range(12)]
    frames = flat_frames(syms)
    trades = [make_trade(s, 2, 10, 100.0, 80.0, 80.0, med=1e9 - k) for k, s in enumerate(syms)]  # ~5% of equity each
    grid = frames[syms[0]].index
    p = bt.portfolio(trades, frames, grid, 1.0)
    st = p["stats"]
    assert st["taken"] == 10 and st["skipped_max_positions"] == 2   # max 10 open
    assert p["min_cash"] >= -1e-6                                      # never negative
    taken = {r["symbol"] for r in p["log"]}
    assert taken == set(syms[:10])                                     # priority: higher known volume first
    assert all(r["pnl_pct_equity"] == pytest.approx(-1.0, abs=0.05) for r in p["log"])  # stop = ~1% of equity


def test_portfolio_cash_limit_reduces_size():
    syms = [f"C{k}USDT" for k in range(8)]
    frames = flat_frames(syms)
    # 1% risk with a stop 0.5% away wants 200% of equity; the 20% cap binds -> 5 positions use all cash
    trades = [make_trade(s, 2, 10, 100.0, 99.5, 99.5) for s in syms]
    p = bt.portfolio(trades, frames, frames[syms[0]].index, 1.0)
    assert p["min_cash"] >= -1e-6 and p["stats"]["reduced_by_cash"] >= 1 and p["stats"]["skipped_no_cash"] >= 1


def test_gap_loss_over_one_percent_is_reported():
    frames = flat_frames(["AUSDT"])
    t = make_trade("AUSDT", 2, 5, 100.0, 80.0, 95.0, kind="stop_gap", phase=0)
    p = bt.portfolio([t], frames, frames["AUSDT"].index, 1.0)
    assert p["stats"]["loss_over_1pct"] == 1 and p["stats"]["worst_trade_pct_equity"] < -3


def test_sma200_benchmark_enters_after_close_above_and_pays_costs():
    closes = [100.0] * 200 + [110.0] * 5 + [90.0] * 5
    btc = frame([(c, c, c, c) for c in closes])
    eq, trips = bt.bench_sma200(btc, btc.index[0], 1e9, 1.0)
    assert trips == 1 and eq.iloc[-1] < bt.START_CAPITAL * 90 / 110   # bought at 110, sold at 90, minus costs
    eq0, _ = bt.bench_sma200(btc, btc.index[0], 1e9, 0.0)
    assert eq0.iloc[-1] == pytest.approx(bt.START_CAPITAL * 90 / 110)


def test_buy_hold_costs():
    btc = frame([(100, 100, 100, 100), (100, 100, 100, 200)])
    assert bt.bench_buy_hold(btc, btc.index[0], 1e9, 0.0).iloc[-1] == pytest.approx(20_000)
    assert bt.bench_buy_hold(btc, btc.index[0], 1e9, 1.0).iloc[-1] < 20_000


def test_research_membership_excludes_stable_like_coins():
    n = 40
    idx = pd.to_datetime([T0 + k * DAY for k in range(n)], unit="ms", utc=True)
    vol = pd.Series(10e6, index=idx)
    coin = pd.DataFrame({"close": np.linspace(1, 2, n), "quote_volume": vol}, index=idx)
    peg = pd.DataFrame({"close": 1.0 + 0.001 * np.sin(np.arange(n)), "quote_volume": vol}, index=idx)
    m = bt.research_membership({"AUSDT": coin, "PEGUSDT": peg},
                               {"volume_lookback_days": 30, "min_median_quote_volume_usd": 5e6, "max_size": 100})
    assert m["AUSDT"].iloc[-1] and not m["PEGUSDT"].any()


def test_wilson_and_metrics():
    lo, hi = bt.wilson(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-3) and hi == pytest.approx(0.5962, abs=1e-3)
    f = frame([(100, 101, 99, 100), (100, 100.5, 94, 95)])
    t, _ = run("A", f, ev_for(f, 0, stop=95))
    m = bt.trade_metrics([t], 1.0)
    assert m["n"] == 1 and m["win_rate"] == 0 and m["exits"] == {"stop": 1}


def test_backtest_refuses_out_of_sample_data(tmp_path, monkeypatch):
    import pickle

    import backtest as script
    ok = frame([(1, 1, 1, 1)] * 3)
    late = ok.copy()
    late["close_time"] = bt.IS_END_MS + 5
    for name, d in (("is_1d.pkl", {"BTCUSDT": ok}), ("is_4h.pkl", {"BTCUSDT": late})):
        (tmp_path / name).write_bytes(pickle.dumps(d))
    monkeypatch.setattr(script, "CACHE", tmp_path)
    with pytest.raises(SystemExit):
        script.load()
