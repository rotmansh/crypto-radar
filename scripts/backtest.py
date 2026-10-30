"""Phase 5a — IN-SAMPLE backtest (docs/PHASE5_PLAN.md). There is no out-of-sample mode in this script.

  python scripts/build_research_dataset.py   # once: archive data up to 2024-12 -> data/research_cache/
  python scripts/backtest.py

Writes reports/backtest/is/<stamp>/: REPORT.md (Hebrew), summary.json, trades/<stream>.csv,
equity/<stream>.csv (daily, 1× costs), benchmarks.csv.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from radar import backtest as bt  # noqa: E402
from radar import config, engine  # noqa: E402
from radar.strategies import ALL, RESEARCH  # noqa: E402
from scan import code_commit  # noqa: E402

CACHE = ROOT / "data/research_cache"
SCEN = bt.COST_SCENARIOS


def load() -> tuple[dict, dict]:
    with open(CACHE / "is_1d.pkl", "rb") as f:
        d1 = pickle.load(f)
    with open(CACHE / "is_4h.pkl", "rb") as f:
        d4 = pickle.load(f)
    for tf, d in (("1d", d1), ("4h", d4)):  # guard: not one candle from the out-of-sample period
        late = [s for s, df in d.items() if len(df) and int(df["close_time"].max()) >= bt.IS_END_MS]
        if late:
            raise SystemExit(f"refusing: {tf} data reaches the out-of-sample period: {late[:5]}")
    return d1, d4


def kind_of(strategy_id: str) -> str:
    return strategy_id[0]


def yearly(trades: list, m: float) -> dict:
    out: dict[int, list] = {}
    for t in trades:
        out.setdefault(pd.Timestamp(t.entry_time, unit="ms").year, []).append(t.r[m])
    return {y: {"n": len(v), "exp_r": float(np.mean(v)), "sum_r": float(np.sum(v))} for y, v in sorted(out.items())}


def vs_btc(eq: pd.Series, btc_eq: pd.Series) -> dict:
    a = eq.groupby(eq.index.normalize()).last().pct_change()
    b = btc_eq.groupby(btc_eq.index.normalize()).last().pct_change()
    j = pd.concat([a, b], axis=1, keys=["s", "b"]).dropna()
    if len(j) < 30 or j["b"].var() == 0:
        return {"corr": math.nan, "beta": math.nan}
    return {"corr": float(j["s"].corr(j["b"])), "beta": float(j["s"].cov(j["b"]) / j["b"].var())}


def main() -> int:
    t_start = dt.datetime.now(dt.timezone.utc)
    settings = config.load_settings()
    ucfg = settings["universe"]
    raw_1d, raw_4h = load()
    member = bt.research_membership(raw_1d, ucfg)
    med = bt.median_volume(raw_1d, ucfg["volume_lookback_days"])
    btc = raw_1d["BTCUSDT"]
    start = bt.window_start(member, btc)
    start_ms = int(start.value // 10**6)
    print("window start", start.date(), "· series 1d", len(raw_1d), "4h", len(raw_4h), flush=True)

    strategies = ALL + RESEARCH
    raw = {"1d": raw_1d, "4h": raw_4h}
    res = engine.run(raw, ucfg, set(raw_1d), ALL, as_of_ms=bt.IS_END_MS, membership=member)
    # The research versions share one id, and engine results are keyed by id: one engine run per version so no
    # version can overwrite another's evaluation.
    evaluated = {(s.ref, tf): res.evaluated[(s.id, tf)] for s in ALL for tf in s.timeframes}
    for v in RESEARCH:
        rv = engine.run(raw, ucfg, set(raw_1d), (v,), as_of_ms=bt.IS_END_MS, membership=member)
        assert rv.data_fingerprint == res.data_fingerprint
        evaluated.update({(v.ref, tf): rv.evaluated[(v.id, tf)] for tf in v.timeframes})
    print("engine done", res.data_fingerprint, flush=True)

    btc_med = float(med["BTCUSDT"].reindex([start]).iloc[0])
    bench = {}
    for m in SCEN:
        bh = bt.bench_buy_hold(btc, start, btc_med, m)
        sma, trips = bt.bench_sma200(btc, start, btc_med, m)
        ew, ew_info = bt.bench_equal_weight(raw_1d, member, med, start, m)
        bench[m] = {"btc_buy_hold": (bh, {}), "btc_sma200": (sma, {"round_trips": trips}),
                    "equal_weight_monthly": (ew, ew_info)}
    btc_bh_1x = bench[1.0]["btc_buy_hold"][0]
    print("benchmarks done", flush=True)

    stamp = t_start.strftime("%Y%m%dT%H%MZ")
    out = ROOT / "reports/backtest/is" / stamp
    (out / "trades").mkdir(parents=True, exist_ok=True)
    (out / "equity").mkdir(parents=True, exist_ok=True)
    streams = {}
    for strat in strategies:
        for tf in strat.timeframes:
            name = f"{strat.ref}·{tf}"
            frames = res.frames[tf]
            trades, skipped = bt.stream_trades(kind_of(strat.id), tf, frames, evaluated[(strat.ref, tf)], med,
                                               name, start_ms)
            grid = frames["BTCUSDT"].index[frames["BTCUSDT"].index >= start]
            s = {"strategy": strat.ref, "params_hash": strat.params_hash, "timeframe": tf,
                 "research": strat in RESEARCH, "skipped": skipped,
                 "trades": {m: bt.trade_metrics(trades, m) for m in SCEN},
                 "no_partial_window_1x": bt.trade_metrics([t for t in trades if not t.partial_in_window], 1.0),
                 "yearly_1x": yearly(trades, 1.0), "portfolio": {}}
            for m in SCEN:
                p = bt.portfolio(trades, frames, grid, m)
                eq = p["equity"]["equity"]
                s["portfolio"][m] = {"curve": bt.curve_metrics(eq), "stats": p["stats"], "min_cash": p["min_cash"],
                                     "vs_btc": vs_btc(eq, btc_bh_1x)}
                if m == 1.0:
                    eq.groupby(eq.index.normalize()).last().to_csv(out / "equity" / f"{strat.id}@{strat.version}_{tf}.csv",
                                                                  header=["equity_usd"])
                    exposure = (p["equity"]["cash"] < p["equity"]["equity"] - 1e-6).mean()
                    s["portfolio"][m]["time_in_market"] = float(exposure)
            with open(out / "trades" / f"{strat.id}@{strat.version}_{tf}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "signal_utc", "entry_utc", "exit_utc", "entry_raw", "stop", "target", "exit_raw",
                            "exit_kind", "bars_held", "slip_1x", "median_qvol_30d", "r_unit_1x", "r_0x", "r_1x", "r_2x",
                            "same_candle_stop_target", "partial_in_window", "high_risk"])
                for t in trades:
                    w.writerow([t.symbol, pd.Timestamp(t.signal_time, unit="ms"), pd.Timestamp(t.entry_time, unit="ms"),
                                pd.Timestamp(t.exit_time, unit="ms"), t.entry_raw, t.stop, t.target, t.exit_raw,
                                t.exit_kind, t.bars_held, t.slip, t.median_qvol, t.r_unit, t.r[0.0], t.r[1.0], t.r[2.0],
                                t.same_candle_stop_target, t.partial_in_window, t.high_risk])
            streams[name] = s
            tm = s["trades"][1.0]
            print(f"{name}: n={tm.get('n')} expR={tm.get('exp_r', float('nan')):.3f}", flush=True)

    # A-TAIL selection on in-sample only (§11)
    selection = {}
    for tf in ("1d", "4h"):
        cands = [(name, s) for name, s in streams.items() if s["research"] and s["timeframe"] == tf
                 and s["trades"][1.0]["n"] >= 100]
        if cands:
            best = max(cands, key=lambda x: (x[1]["trades"][1.0]["exp_r"], -float(x[0].split("@0.")[1][0])))
            selection[tf] = {"selected": best[0], "params_hash": best[1]["params_hash"],
                             "exp_r_1x": best[1]["trades"][1.0]["exp_r"], "n": best[1]["trades"][1.0]["n"]}
        else:
            selection[tf] = {"selected": None, "reason": "no variant with >= 100 in-sample trades"}

    bench_out = {str(m): {k: {"curve": bt.curve_metrics(v[0]), **v[1]} for k, v in b.items()} for m, b in bench.items()}
    pd.DataFrame({k: v[0] for k, v in bench[1.0].items()}).to_csv(out / "benchmarks_1x.csv")
    ds = sorted((ROOT / "reports/backtest/dataset").glob("*/dataset.json"))
    dataset = json.loads(ds[-1].read_text()) if ds else {}
    summary = {
        "period": "in_sample", "window_start_utc": str(start), "data_end_utc": "2025-01-01T00:00:00Z",
        "generated_utc": t_start.isoformat(timespec="seconds"), "code_commit": code_commit(),
        "data_fingerprint": res.data_fingerprint, "dataset": ds[-1].parent.name if ds else None,
        "series_1d": len(raw_1d), "series_4h": len(raw_4h),
        "universe_size_at_start": int(member.loc[start].sum()),
        "universe_size_median": float(member[member.index >= start].sum(axis=1).median()),
        "streams": streams, "benchmarks": bench_out, "tail_selection": selection,
        "survivorship": {"ever_members": dataset.get("ever_members"),
                         "ever_members_not_in_live_store": dataset.get("ever_members_not_in_live_store"),
                         "cross_check_vs_api": dataset.get("cross_check_vs_api")},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    from backtest_report import render
    (out / "REPORT.md").write_text(render(summary))
    print("report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
