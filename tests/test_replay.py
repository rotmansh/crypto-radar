"""Time Machine guarantees (SPEC §9): no future data, point-in-time universe, determinism, one engine.

The candle stores here are TEST FIXTURES written to a temporary directory.
"""
import dataclasses
import json
import random

import numpy as np
import pandas as pd
import pytest
from conftest import DAY, make_candles

from radar import engine, replay
from radar.store import CandleStore
from radar.strategies import ALL, SIGNAL, WATCH

START = 1_577_836_800_000  # 2020-01-01
SETTINGS = {"universe": {"min_median_quote_volume_usd": 20_000, "volume_lookback_days": 30, "max_size": 100}}
N = 420


def build_store(tmp_path, n=N, coins=14, quiet_until=None):
    """14 coins + BTC. Coin Q0 trades below the volume floor until `quiet_until` (index), then far above it."""
    st = CandleStore(tmp_path, "binance")
    st.append("BTCUSDT", "1d", make_candles(n, DAY, START, seed=500, start_price=10_000))
    for i in range(coins):
        c = make_candles(n, DAY, START, seed=i, start_price=50 + i)
        if i == 0 and quiet_until is not None:
            c = [dataclasses.replace(x, quote_volume=x.quote_volume * (0.01 if k < quiet_until else 50))
                 for k, x in enumerate(c)]
        st.append(f"Q{i}USDT", "1d", c)
    return st


def run_replay(st, as_of_ms, tf="1d"):
    return replay.replay(st, SETTINGS, {}, {}, as_of_ms, tf)


def as_of_index(k):
    return START + (k + 1) * DAY  # moment candle k closes


def test_future_changes_do_not_change_replay(tmp_path):
    st = build_store(tmp_path / "a")
    as_of = as_of_index(300)
    before = run_replay(st, as_of)
    # A second store identical up to candle 300, with wildly different candles afterwards.
    st2 = CandleStore(tmp_path / "b", "binance")
    for sym in st.symbols("1d"):
        c = st.load(sym, "1d")
        rng = random.Random(sym)
        future = [dataclasses.replace(x, open=x.open * 3, high=x.high * 5, low=x.low * 0.2, close=x.close * rng.uniform(0.1, 9),
                                      quote_volume=x.quote_volume * 100) for x in c[301:]]
        future = [dataclasses.replace(x, high=max(x.open, x.close, x.high), low=min(x.open, x.close, x.low)) for x in future]
        st2.append(sym, "1d", c[:301] + future)
    after = run_replay(st2, as_of)
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)


def _timestamps(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_utc") and isinstance(v, str):
                out.append((k, v))
            _timestamps(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _timestamps(v, out)
    return out


def test_replay_output_contains_nothing_after_as_of(tmp_path):
    st = build_store(tmp_path)
    for k in (250, 333, 400):
        as_of = as_of_index(k)
        r = run_replay(st, as_of)
        limit = replay.iso(as_of)
        for key, ts in _timestamps(r, []):
            assert ts <= limit, (key, ts, limit)
        for b in r["results"]:
            for it in b.get("signals", []) + b.get("watch", []):
                assert it["candle_close_utc"] <= limit
        assert r["last_available_candle_utc"] == replay.iso(START + k * DAY)


def test_candle_closing_exactly_at_as_of_is_available_one_ms_earlier_is_not(tmp_path):
    st = build_store(tmp_path)
    close_of_300 = as_of_index(300)
    assert run_replay(st, close_of_300)["last_available_candle_utc"] == replay.iso(START + 300 * DAY)
    assert run_replay(st, close_of_300 - 1)["last_available_candle_utc"] == replay.iso(START + 299 * DAY)


def test_replay_is_deterministic(tmp_path):
    st = build_store(tmp_path)
    a, b = run_replay(st, as_of_index(350)), run_replay(st, as_of_index(350))
    assert a == b and a["data_fingerprint"] == b["data_fingerprint"]


def test_point_in_time_universe_does_not_use_todays_membership(tmp_path):
    st = build_store(tmp_path, quiet_until=330)  # Q0 is tiny until candle 330, huge today
    early = run_replay(st, as_of_index(320))
    late = run_replay(st, as_of_index(380))
    assert "Q0USDT" not in early["universe_at_as_of"]
    assert "Q0USDT" in late["universe_at_as_of"]
    # And it receives no signal/watch while it was not a member.
    for b in early["results"]:
        assert all(x["symbol"] != "Q0USDT" for x in b.get("signals", []) + b.get("watch", []))


def test_c_rank_uses_only_members_of_that_date(tmp_path):
    st = build_store(tmp_path, quiet_until=330)
    pool = st.symbols("1d")
    as_of = as_of_index(320)
    raw = {"1d": engine.load_candles(st, pool, "1d", as_of)}
    res = engine.run(raw, SETTINGS["universe"], set(pool), ALL, as_of_ms=as_of)
    c = res.evaluated[("C-TRS", "1d")]
    members = res.membership.iloc[-1]
    n_members = int(members.drop("BTCUSDT", errors="ignore").sum())
    q1 = c["Q1USDT"]
    rs_n = q1["rs_universe_n"].iloc[-1] if "rs_universe_n" in q1 else None
    assert rs_n is None or np.isnan(rs_n) or rs_n <= n_members
    assert not bool(members.get("Q0USDT", False))


def test_replay_equals_full_history_timeline(tmp_path):
    """The site's precomputed timeline (engine on all data) must equal a true truncated replay at every as_of."""
    st = build_store(tmp_path, quiet_until=330)
    pool = st.symbols("1d")
    full = engine.run({"1d": engine.load_candles(st, pool, "1d")}, SETTINGS["universe"], set(pool), ALL)
    for k in (260, 329, 331, 377, N - 1):
        as_of = as_of_index(k)
        part = engine.run({"1d": engine.load_candles(st, pool, "1d", as_of)}, SETTINGS["universe"], set(pool), ALL,
                          as_of_ms=as_of)
        for key, per in part.evaluated.items():
            for sym, ev in per.items():
                f = full.evaluated[key][sym]
                assert f["state"].iloc[len(ev) - 1] == ev["state"].iloc[-1], (key, sym, k)
                assert f["state"].iloc[: len(ev)].tolist() == ev["state"].tolist(), (key, sym, k)


def test_gaps_are_not_filled(tmp_path):
    st = CandleStore(tmp_path, "binance")
    full = make_candles(N, DAY, START, seed=7)
    st.append("BTCUSDT", "1d", make_candles(N, DAY, START, seed=500, start_price=10_000))
    st.append("GAPUSDT", "1d", full[:300] + full[305:])  # 5 missing candles
    r = run_replay(st, as_of_index(302))
    for b in r["results"]:
        assert all(x["symbol"] != "GAPUSDT" for x in b.get("signals", []) + b.get("watch", []))
        if b.get("available"):
            assert any(s["symbol"] == "GAPUSDT" for s in b["stale"])  # reported, not interpolated


def test_strategy_selection_and_c_only_daily(tmp_path):
    st = build_store(tmp_path)
    r = run_replay(st, as_of_index(350), "1d")
    assert [b["strategy"] for b in r["results"]] == ["A-SMA50PB", "B-BBSQ", "C-TRS"]
    only_b = replay.replay(st, SETTINGS, {}, {}, as_of_index(350), "1d", ("B-BBSQ",))
    assert [b["strategy"] for b in only_b["results"]] == ["B-BBSQ"]
    assert only_b["reveal"]["available"] is False  # results engine arrives in phase 5; nothing invented


def test_engine_itself_truncates_when_given_full_data(tmp_path):
    """Second line of defence: even if a caller passes untruncated candles, the engine cuts them at as_of."""
    st = build_store(tmp_path, quiet_until=330)
    pool = st.symbols("1d")
    as_of = as_of_index(320)
    from_full = engine.run({"1d": engine.load_candles(st, pool, "1d")}, SETTINGS["universe"], set(pool), ALL,
                           as_of_ms=as_of)
    from_cut = engine.run({"1d": engine.load_candles(st, pool, "1d", as_of)}, SETTINGS["universe"], set(pool), ALL,
                          as_of_ms=as_of)
    assert from_full.data_fingerprint == from_cut.data_fingerprint
    pd.testing.assert_frame_equal(from_full.membership, from_cut.membership)
    for key, per in from_cut.evaluated.items():
        for sym, ev in per.items():
            pd.testing.assert_frame_equal(from_full.evaluated[key][sym], ev)
