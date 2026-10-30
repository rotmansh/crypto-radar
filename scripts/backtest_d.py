"""Strategy D — IN-SAMPLE research run (docs/STRATEGY_D_SPEC.md). There is no out-of-sample mode.

  python scripts/build_research_dataset.py && python scripts/build_research_1h.py   # data up to 2024-12
  python scripts/backtest_d.py

12 streams (6 management versions × 1h, 4h), the same entries for every version; paired comparisons
(break-even vs not, trail vs not), in-sample selection per timeframe, random-entry control for the selected
version, benchmarks, and strategy A 4h shown alongside (from the phase 5a report, unchanged).
Writes reports/backtest/d_is/<stamp>/.
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
from radar.strategies import SWING_RESEARCH  # noqa: E402
from radar.strategies.sma50_swing import SMA50Swing  # noqa: E402
from scan import code_commit  # noqa: E402

CACHE = ROOT / "data/research_cache"
SCEN = bt.COST_SCENARIOS
HOURS = {"1h": 1, "4h": 4}
SIMPLICITY = ["0.4.0", "0.1.0", "0.5.0", "0.2.0", "0.6.0", "0.3.0"]
MC_SIMS, MC_SEED = 1000, 7
PAIRS_BE = [("0.1.0", "0.4.0"), ("0.2.0", "0.5.0"), ("0.3.0", "0.6.0")]
PAIRS_TRAIL = [("0.2.0", "0.1.0"), ("0.3.0", "0.1.0"), ("0.5.0", "0.4.0"), ("0.6.0", "0.4.0")]


def load() -> dict:
    out = {}
    for tf in ("1d", "4h", "1h"):
        with open(CACHE / f"is_{tf}.pkl", "rb") as f:
            d = pickle.load(f)
        late = [s for s, df in d.items() if len(df) and int(df["close_time"].max()) >= bt.IS_END_MS]
        if late:
            raise SystemExit(f"refusing: {tf} data reaches the out-of-sample period: {late[:5]}")
        out[tf] = d
    return out


def entry_params(s) -> dict:
    return {k: v for k, v in s.params.items() if k not in ("break_even_after_1r", "trail_atr")}


def paired(a: list, b: list) -> dict:
    """Mean R difference (a − b, 1× costs) over entries both versions took (same coin, same entry candle)."""
    ra = {(t.symbol, t.entry_time): t.r[1.0] for t in a}
    rb = {(t.symbol, t.entry_time): t.r[1.0] for t in b}
    common = sorted(set(ra) & set(rb))
    d = np.array([ra[k] - rb[k] for k in common])
    if not len(d):
        return {"n": 0}
    lo, hi = bt.bootstrap_ci(d)
    return {"n": len(d), "mean_diff": float(d.mean()), "ci": (lo, hi), "a_better": int((d > 1e-12).sum()),
            "b_better": int((d < -1e-12).sum())}


def hold_profile(trades: list, tf: str) -> dict:
    hrs = np.array([t.bars_held * HOURS[tf] for t in trades])
    r = np.array([t.r[1.0] for t in trades])
    if not len(hrs):
        return {}
    out = {"median_hours": float(np.median(hrs)), "mean_hours": float(hrs.mean())}
    for name, lo, hi in (("upto_24h", 0, 24), ("24_72h", 24, 72), ("over_72h", 72, 10**9)):
        m = (hrs > lo) & (hrs <= hi) if lo else (hrs <= hi)
        out[name] = {"n": int(m.sum()), "exp_r": float(r[m].mean()) if m.any() else math.nan,
                     "sum_r": float(r[m].sum())}
    return out


def filtered_counts(frames: dict, evaluated: dict, start_ms: int) -> dict:
    """Candles that met every signal rule except the stop-width or the cost rule (SPEC D §5.4)."""
    s = SMA50Swing("0.1.0")
    others = [f"c_{k}" for k in s.signal_rules if k not in ("stop_not_too_wide", "risk_vs_cost")]
    wide = cost = 0
    for sym, ev in evaluated.items():
        in_window = frames[sym]["open_time"].to_numpy() >= start_ms
        ok = (ev[others] == 1).all(axis=1).to_numpy() & ev["member"].to_numpy() & in_window
        wide += int((ok & (ev["c_stop_not_too_wide"] == 0).to_numpy()).sum())
        cost += int((ok & (ev["c_stop_not_too_wide"] == 1).to_numpy() & (ev["c_risk_vs_cost"] == 0).to_numpy()).sum())
    return {"stop_too_wide": wide, "risk_below_4x_cost": cost}


def monte_carlo(tf: str, frames: dict, evaluated: dict, med: pd.DataFrame, n_trades: int, mgmt: dict,
                start_ms: int) -> dict:
    """Random entries (SPEC D §7): candles where the coin is a member and passes the $20M rule, with a valid stop
    that passes the width and cost rules; same management and costs. Mean R at 1× per simulation."""
    pool = []
    for sym, ev in evaluated.items():
        ok = (ev["member"].to_numpy() & (ev["c_liquid"] == 1).to_numpy() & (ev["c_stop_not_too_wide"] == 1).to_numpy()
              & (ev["c_risk_vs_cost"] == 1).to_numpy() & (frames[sym]["open_time"].to_numpy() >= start_ms))
        idx = np.flatnonzero(ok)
        idx = idx[idx + 1 < len(ev)]
        pool += [(sym, int(i)) for i in idx]
    rng = np.random.default_rng(MC_SEED)
    memo: dict = {}
    meds = {s: bt.align_daily(frames[s], med[s]) if s in med else None for s in evaluated}

    def r_of(sym, i):
        if (sym, i) not in memo:
            f, ev = frames[sym], evaluated[sym]
            ends = int(f["close_time"].iloc[-1]) >= bt.IS_END_MS - bt.INTERVAL_MS[tf] - 1
            t, _ = bt.simulate_d(tf, f, ev, i, float(meds[sym][i]), "MC", sym, ends, mgmt)
            memo[(sym, i)] = t.r[1.0] if t else None
        return memo[(sym, i)]
    means = []
    for _ in range(MC_SIMS):
        rs = []
        while len(rs) < n_trades:
            for k in rng.choice(len(pool), size=n_trades - len(rs), replace=False):
                r = r_of(*pool[k])
                if r is not None:
                    rs.append(r)
        means.append(float(np.mean(rs)))
    return {"sims": MC_SIMS, "pool": len(pool), "mean_of_means": float(np.mean(means)),
            "p5": float(np.percentile(means, 5)), "p95": float(np.percentile(means, 95)), "means": means}


def main() -> int:
    t_start = dt.datetime.now(dt.timezone.utc)
    ucfg = config.load_settings()["universe"]
    raw = load()
    member = bt.research_membership(raw["1d"], ucfg)
    med = bt.median_volume(raw["1d"], ucfg["volume_lookback_days"])
    start = bt.window_start(member, raw["1d"]["BTCUSDT"])
    start_ms = int(start.value // 10**6)
    print("window start", start.date(), {tf: len(d) for tf, d in raw.items()}, flush=True)

    base = SWING_RESEARCH[0]
    assert all(entry_params(s) == entry_params(base) for s in SWING_RESEARCH)  # one entry rule set
    res = engine.run(raw, ucfg, set(raw["1d"]), (base,), as_of_ms=bt.IS_END_MS, membership=member)
    print("engine done", res.data_fingerprint, flush=True)

    btc = raw["1d"]["BTCUSDT"]
    btc_med = float(med["BTCUSDT"].reindex([start]).iloc[0])
    bh = bt.bench_buy_hold(btc, start, btc_med, 1.0)
    sma, trips = bt.bench_sma200(btc, start, btc_med, 1.0)
    ew, _ = bt.bench_equal_weight(raw["1d"], member, med, start, 1.0)
    bench = {"btc_buy_hold": bt.curve_metrics(bh), "btc_sma200": bt.curve_metrics(sma) | {"round_trips": trips},
             "equal_weight_monthly": bt.curve_metrics(ew)}

    stamp = t_start.strftime("%Y%m%dT%H%MZ")
    out = ROOT / "reports/backtest/d_is" / stamp
    (out / "trades").mkdir(parents=True, exist_ok=True)
    (out / "equity").mkdir(parents=True, exist_ok=True)
    streams, by_tf, filt = {}, {}, {}
    for tf in ("1h", "4h"):
        frames, evaluated = res.frames[tf], res.evaluated[(base.id, tf)]
        filt[tf] = filtered_counts(frames, evaluated, start_ms)
        grid = frames["BTCUSDT"].index[frames["BTCUSDT"].index >= start]
        for strat in SWING_RESEARCH:
            name = f"{strat.ref}·{tf}"
            trades, skipped = bt.stream_trades("D", tf, frames, evaluated, med, name, start_ms, mgmt=strat.params)
            s = {"strategy": strat.ref, "version": strat.version, "params_hash": strat.params_hash, "timeframe": tf,
                 "break_even": strat.params["break_even_after_1r"], "trail_atr": strat.params["trail_atr"],
                 "skipped": skipped, "trades": {m: bt.trade_metrics(trades, m) for m in SCEN},
                 "hold": hold_profile(trades, tf), "portfolio": {}}
            for m in SCEN:
                p = bt.portfolio(trades, frames, grid, m)
                eq = p["equity"]["equity"]
                s["portfolio"][m] = {"curve": bt.curve_metrics(eq), "stats": p["stats"], "min_cash": p["min_cash"]}
                if m == 1.0:
                    s["portfolio"][m]["time_in_market"] = float((p["equity"]["cash"] < p["equity"]["equity"] - 1e-6).mean())
                    eq.groupby(eq.index.normalize()).last().to_csv(out / "equity" / f"{strat.id}@{strat.version}_{tf}.csv",
                                                                  header=["equity_usd"])
            with open(out / "trades" / f"{strat.id}@{strat.version}_{tf}.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "signal_utc", "entry_utc", "exit_utc", "entry_raw", "stop", "exit_raw", "exit_kind",
                            "bars_held", "hours_held", "slip_1x", "median_qvol_30d", "r_unit_1x", "r_0x", "r_1x", "r_2x"])
                for t in trades:
                    w.writerow([t.symbol, pd.Timestamp(t.signal_time, unit="ms"), pd.Timestamp(t.entry_time, unit="ms"),
                                pd.Timestamp(t.exit_time, unit="ms"), t.entry_raw, t.stop, t.exit_raw, t.exit_kind,
                                t.bars_held, t.bars_held * HOURS[tf], t.slip, t.median_qvol, t.r_unit, t.r[0.0],
                                t.r[1.0], t.r[2.0]])
            streams[name] = s
            by_tf.setdefault(tf, {})[strat.version] = trades
            tm = s["trades"][1.0]
            print(f"{name}: n={tm.get('n')} expR={tm.get('exp_r', float('nan')):.3f}", flush=True)

    comparisons, selection, mc = {}, {}, {}
    for tf in ("1h", "4h"):
        tr = by_tf[tf]
        comparisons[tf] = {"break_even": {f"{a} vs {b}": paired(tr[a], tr[b]) for a, b in PAIRS_BE},
                           "trail": {f"{a} vs {b}": paired(tr[a], tr[b]) for a, b in PAIRS_TRAIL}}
        cands = {v: streams[f"D-SMA50SWING@{v}·{tf}"]["trades"][1.0] for v in SIMPLICITY}
        cands = {v: t for v, t in cands.items() if t.get("n", 0) >= 200}
        if not cands:
            selection[tf] = {"selected": None, "reason": "no version with >= 200 in-sample trades"}
        elif max(t["exp_r"] for t in cands.values()) <= 0:
            selection[tf] = {"selected": None, "reason": "every version loses at 1× costs — no in-sample edge",
                             "best": max(cands, key=lambda v: cands[v]["exp_r"])}
        else:
            best = max(t["exp_r"] for t in cands.values())
            v = next(v for v in SIMPLICITY if v in cands and cands[v]["exp_r"] >= best - 0.005)
            selection[tf] = {"selected": f"D-SMA50SWING@{v}·{tf}", "version": v,
                             "params_hash": SMA50Swing(v).params_hash, "exp_r_1x": cands[v]["exp_r"], "n": cands[v]["n"]}
        # random-entry control for the selected version (or, if none, for the best one, for information)
        v = selection[tf].get("version") or selection[tf].get("best")
        if v:
            n = streams[f"D-SMA50SWING@{v}·{tf}"]["trades"][1.0]["n"]
            r = monte_carlo(tf, res.frames[tf], res.evaluated[(base.id, tf)], med, n, SMA50Swing(v).params, start_ms)
            actual = streams[f"D-SMA50SWING@{v}·{tf}"]["trades"][1.0]["exp_r"]
            r["version"], r["actual_exp_r"] = v, actual
            r["percentile_of_actual"] = float((np.array(r.pop("means")) < actual).mean() * 100)
            mc[tf] = r
            print(f"MC {tf} {v}: actual {actual:.3f} percentile {r['percentile_of_actual']:.1f}", flush=True)

    a5 = sorted((ROOT / "reports/backtest/is").glob("*/summary.json"))
    a4h = None
    if a5:
        a = json.loads(a5[-1].read_text())["streams"].get("A-SMA50PB@1.0.0·4h")
        if a:
            a4h = {"report": a5[-1].parent.name, "trades": a["trades"]["1.0"], "portfolio": a["portfolio"]["1.0"]["curve"]}
    ds = sorted((ROOT / "reports/backtest/dataset_1h").glob("*/dataset.json"))
    summary = {
        "period": "in_sample", "window_start_utc": str(start), "data_end_utc": "2025-01-01T00:00:00Z",
        "generated_utc": t_start.isoformat(timespec="seconds"), "code_commit": code_commit(),
        "data_fingerprint": res.data_fingerprint, "dataset_1h": ds[-1].parent.name if ds else None,
        "series": {tf: len(d) for tf, d in raw.items()}, "streams": streams, "filtered_signals": filt,
        "comparisons": comparisons, "selection": selection, "monte_carlo": mc, "benchmarks_1x": bench,
        "a_4h_phase5a": a4h,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    from backtest_d_report import render
    (out / "REPORT.md").write_text(render(summary))
    print("report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
