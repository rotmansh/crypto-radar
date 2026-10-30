"""Phase 5 backtest — trade simulation, virtual portfolios, benchmarks and metrics (docs/PHASE5_PLAN.md).

Signals come only from radar.engine (the same engine as the live scan and the Time Machine). This module adds
what happens AFTER a confirmed signal: fills, exits, costs, R, position sizing and cash. Every rule here is the
pre-declared rule of the plan; the section numbers below refer to it.

Path vs. costs: a trade's path (entry candle, exit candle, exit kind, raw prices) does not depend on costs. Costs
are applied afterwards per scenario (0×, 1×, 2×), so the three scenarios hold exactly the same trades.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import INTERVAL_MS

IS_END_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z — in-sample data ends before this instant (§1)
FEE = 0.001  # per side (§6)
SLIP_TIERS = ((500e6, 0.0002), (50e6, 0.0005), (10e6, 0.0010), (0.0, 0.0020))  # per side, by 30d median $ volume
COST_SCENARIOS = (0.0, 1.0, 2.0)
TIME_EXIT = {"1d": 20, "4h": 30}  # A and B (§4)
C_TRAIL_ATR = 3.0
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
STABLE_RANGE_PCT = 3.0
START_CAPITAL = 10_000.0
RISK_PER_TRADE = 0.01
MAX_POSITION = 0.20
MAX_OPEN = 10
MIN_NOTIONAL = 10.0
MIN_UNIVERSE_FOR_WINDOW = 20
DAY_MS = 86_400_000


def slippage(median_qvol: float) -> float:
    if median_qvol is None or not np.isfinite(median_qvol):
        return SLIP_TIERS[-1][1]
    return next(s for floor, s in SLIP_TIERS if median_qvol >= floor)


# ---------------------------------------------------------------- universe (§2)

def median_volume(raw_1d: dict[str, pd.DataFrame], lookback: int) -> pd.DataFrame:
    return pd.DataFrame({s: df["quote_volume"].rolling(lookback, min_periods=lookback).median()
                         for s, df in raw_1d.items()})


def research_membership(raw_1d: dict[str, pd.DataFrame], ucfg: dict) -> pd.DataFrame:
    """Live rule (median 30d $ volume >= floor, >= 30 days, top `max_size`) plus the point-in-time stable-like
    check: a coin whose 30-day close range is under 3% on a date is not a member on that date."""
    lb = ucfg["volume_lookback_days"]
    med = median_volume(raw_1d, lb)
    rng = pd.DataFrame({s: (df["close"].rolling(lb, min_periods=lb).max()
                            / df["close"].rolling(lb, min_periods=lb).min() - 1) * 100
                        for s, df in raw_1d.items()})
    eligible = (med >= ucfg["min_median_quote_volume_usd"]) & (rng >= STABLE_RANGE_PCT)
    rank = med.where(eligible).rank(axis=1, ascending=False, method="first")
    return eligible & (rank <= ucfg["max_size"])


def align_daily(frame: pd.DataFrame, daily: pd.Series) -> np.ndarray:
    """Value of a daily series as known at each candle's close: the latest daily candle closed by then."""
    day_open = ((frame["close_time"].to_numpy() + 1) // DAY_MS - 1) * DAY_MS
    idx = pd.to_datetime(day_open, unit="ms", utc=True)
    return daily.reindex(idx).to_numpy(dtype=float)


def window_start(membership: pd.DataFrame, btc_1d: pd.DataFrame) -> pd.Timestamp:
    """First day with >= 20 universe members and >= 200 BTC daily candles (§1)."""
    ok = membership.sum(axis=1) >= MIN_UNIVERSE_FOR_WINDOW
    btc_ready = btc_1d.index[199] if len(btc_1d) >= 200 else btc_1d.index[-1]
    days = ok.index[ok & (ok.index >= btc_ready)]
    return days[0]


# ---------------------------------------------------------------- one trade (§4, §5, §7)

@dataclass
class Trade:
    stream: str
    symbol: str
    signal_i: int
    signal_time: int
    entry_i: int
    entry_time: int
    entry_raw: float
    stop: float
    target: float
    slip: float  # per side at 1×
    median_qvol: float
    r_unit: float  # planned risk per unit at 1× costs (fees + slippage included)
    exit_i: int = -1
    exit_time: int = 0
    exit_raw: float = math.nan
    exit_kind: str = ""
    exit_phase: int = 0  # 0 at the open, 2 inside the candle, 3 at the close (portfolio event order)
    same_candle_stop_target: bool = False
    partial_in_window: bool = False
    high_risk: bool = False
    r: dict = field(default_factory=dict)  # cost multiplier -> R

    def eff(self, m: float) -> tuple[float, float, float]:
        s, f = self.slip * m, FEE * m
        entry = self.entry_raw * (1 + s)
        exit_ = self.exit_raw * (1 - s)
        return entry, exit_, f

    def net_per_unit(self, m: float) -> float:
        entry, exit_, f = self.eff(m)
        return exit_ - entry - f * entry - f * exit_

    @property
    def bars_held(self) -> int:
        return self.exit_i - self.entry_i + (0 if self.exit_phase == 0 else 1)


def planned_risk(entry_raw: float, stop: float, slip: float) -> float:
    """R_unit = (entry_eff − stop_eff) + fee on both legs, at 1× costs (§7)."""
    entry, stop_eff = entry_raw * (1 + slip), stop * (1 - slip)
    return (entry - stop_eff) + FEE * entry + FEE * stop_eff


def simulate(kind: str, tf: str, f: pd.DataFrame, ev: pd.DataFrame, i: int, median_qvol: float,
             stream: str, symbol: str, series_ends_at_is_end: bool, high_risk_pct: float = 10.0
             ) -> tuple[Trade | None, str | None]:
    """Follow one SIGNAL at candle i to its exit. kind: 'A', 'B' or 'C'. Returns (trade, None) or (None, reason).
    Every decision at candle j reads candle j and earlier only."""
    n = len(f)
    if i + 1 >= n:
        return None, "no_next_candle"
    o, h, lo, c = (f[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    stop = float(ev["stop"].iloc[i])
    e = i + 1
    entry = o[e]
    if not np.isfinite(stop) or entry <= stop:
        return None, "gap_below_stop"
    if kind == "A":
        target = entry + 2 * (entry - stop)
    elif kind == "B":
        target = entry + float(ev["range_high"].iloc[i] - ev["range_low"].iloc[i])
        if not target > entry:
            return None, "target_at_or_below_entry"
    else:
        target = math.nan
    s = slippage(median_qvol)
    r_unit = planned_risk(entry, stop, s)
    if not r_unit > 0:
        return None, "no_risk"
    close_sig = float(f["close"].iloc[i])
    t = Trade(stream, symbol, i, int(f["open_time"].iloc[i]), e, int(f["open_time"].iloc[e]), entry, stop, target,
              s, median_qvol, r_unit,
              partial_in_window=bool(f["partial"].iloc[max(0, i - 19): i + 1].any()),
              high_risk=(close_sig - stop) / close_sig * 100 > high_risk_pct)

    def done(j, price, how, phase, both=False):
        t.exit_i, t.exit_time, t.exit_raw, t.exit_kind, t.exit_phase = j, int(f["open_time"].iloc[j]), price, how, phase
        t.same_candle_stop_target = both
        return apply_costs(t), None

    hold_n = TIME_EXIT[tf]
    sma50 = f["sma50"].to_numpy(dtype=float) if kind == "C" else None
    atr = f["atr14"].to_numpy(dtype=float) if kind == "C" else None
    pending, max_close = None, -math.inf
    for j in range(e, n):
        # at the open
        if pending:
            return done(j, o[j], pending, 0)
        if kind in ("A", "B") and j - e >= hold_n:
            return done(j, o[j], "time", 0)
        if o[j] <= stop:
            return done(j, o[j], "stop_gap", 0)
        # inside the candle: stop first when both levels are touched (§4)
        hit_target = kind != "C" and h[j] >= target
        if lo[j] <= stop:
            return done(j, stop, "stop", 2, both=hit_target)
        if hit_target:
            return done(j, target, "target", 2)
        # at the close (C only): trailing stop and trend exit, executed at the next open
        if kind == "C":
            max_close = max(max_close, c[j])
            trail = max_close - C_TRAIL_ATR * atr[j]
            if c[j] < trail:
                pending = "trail"
            elif c[j] < sma50[j]:
                pending = "sma50"
    return done(n - 1, c[n - 1], "period_end" if series_ends_at_is_end else "data_end", 3)


def apply_costs(t: Trade) -> Trade:
    t.r = {m: t.net_per_unit(m) / t.r_unit for m in COST_SCENARIOS}
    return t


def stream_trades(kind: str, tf: str, frames: dict, evaluated: dict, med_daily: pd.DataFrame, stream: str,
                  start_ms: int) -> tuple[list[Trade], dict]:
    """Level 1: every member SIGNAL from `start_ms` on, one open trade per coin at a time (§3)."""
    trades, skipped = [], {}
    for sym, ev in evaluated.items():
        f = frames[sym]
        sig = np.flatnonzero(ev["state"].to_numpy() == "SIGNAL")
        if not len(sig):
            continue
        med = align_daily(f, med_daily[sym]) if sym in med_daily else np.full(len(f), np.nan)
        ends_at_is_end = int(f["close_time"].iloc[-1]) >= IS_END_MS - INTERVAL_MS[tf] - 1
        busy_until = -1
        for i in sig:
            if int(f["open_time"].iloc[i]) < start_ms:
                continue
            if i + 1 <= busy_until:
                skipped["already_in_position"] = skipped.get("already_in_position", 0) + 1
                continue
            t, why = simulate(kind, tf, f, ev, int(i), float(med[i]), stream, sym, ends_at_is_end)
            if t is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            trades.append(t)
            busy_until = t.exit_i
    trades.sort(key=lambda t: (t.entry_time, -np.nan_to_num(t.median_qvol), t.symbol))
    return trades, skipped


# ---------------------------------------------------------------- portfolio (§8)

def portfolio(trades: list[Trade], frames: dict, grid: pd.DatetimeIndex, m: float) -> dict:
    """One virtual portfolio for one stream at cost multiplier m. Cash is real: never negative, no leverage."""
    closes = {s: (frames[s]["open_time"].to_numpy(dtype=np.int64), frames[s]["close"].to_numpy(dtype=float))
              for s in {t.symbol for t in trades}}
    events: dict[tuple[int, int], list] = {}
    for t in trades:
        events.setdefault((t.entry_time, 1), []).append(("in", t))
        events.setdefault((t.exit_time, t.exit_phase), []).append(("out", t))
    for v in events.values():  # entries in priority order: higher known volume first (§8)
        v.sort(key=lambda e: (e[0] != "out", -np.nan_to_num(e[1].median_qvol), e[1].symbol))
    keys = sorted(events)
    cash, open_pos, log = START_CAPITAL, {}, []
    stats = {"taken": 0, "skipped_max_positions": 0, "skipped_no_cash": 0, "reduced_by_cash": 0, "fees_usd": 0.0,
             "slippage_usd": 0.0, "loss_over_1pct": 0, "worst_trade_pct_equity": 0.0}

    def mark(sym, ts):
        """Close of the latest candle of `sym` that opened at or before `ts` (ms)."""
        times, px = closes[sym]
        k = np.searchsorted(times, ts, side="right") - 1
        return float(px[k])

    equity_curve, ki = [], 0
    for ts in grid:
        t_ms = int(ts.value // 10**6)
        while ki < len(keys) and keys[ki][0] <= t_ms:
            for what, t in events[keys[ki]]:
                key = (t.symbol, t.entry_time)
                if what == "out":
                    p = open_pos.pop(key, None)
                    if p is None:
                        continue
                    entry_eff, exit_eff, f = t.eff(m)
                    proceeds = p["units"] * exit_eff
                    fee = proceeds * f
                    cash += proceeds - fee
                    pnl = p["units"] * t.net_per_unit(m)
                    stats["fees_usd"] += fee
                    stats["slippage_usd"] += p["units"] * t.exit_raw * t.slip * m
                    pct = pnl / p["equity_at_entry"] * 100
                    stats["worst_trade_pct_equity"] = min(stats["worst_trade_pct_equity"], pct)
                    if pct < -RISK_PER_TRADE * 100 - 1e-9:
                        stats["loss_over_1pct"] += 1
                    log.append({"symbol": t.symbol, "entry_time": t.entry_time, "exit_time": t.exit_time,
                                "units": p["units"], "pnl_usd": pnl, "pnl_pct_equity": pct, "r": t.r[m],
                                "exit_kind": t.exit_kind, "reduced_by_cash": p["reduced"]})
                else:
                    if len(open_pos) >= MAX_OPEN:
                        stats["skipped_max_positions"] += 1
                        continue
                    equity = cash + sum(p["units"] * mark(sym, t.entry_time - 1) for (sym, _), p in open_pos.items())
                    entry_eff, _, f = t.eff(m)
                    by_risk = RISK_PER_TRADE * equity / t.r_unit
                    by_cap = MAX_POSITION * equity / entry_eff
                    by_cash = cash / (entry_eff * (1 + f))
                    units = min(by_risk, by_cap, by_cash)
                    if units * entry_eff < MIN_NOTIONAL:
                        stats["skipped_no_cash"] += 1
                        continue
                    reduced = by_cash < min(by_risk, by_cap)
                    stats["reduced_by_cash"] += reduced
                    cost = units * entry_eff
                    fee = cost * f
                    cash -= cost + fee
                    stats["fees_usd"] += fee
                    stats["slippage_usd"] += units * t.entry_raw * t.slip * m
                    stats["taken"] += 1
                    open_pos[(t.symbol, t.entry_time)] = {"units": units, "entry_eff": entry_eff,
                                                          "equity_at_entry": equity, "reduced": reduced}
                    assert cash >= -1e-6, "cash went negative"
            ki += 1
        equity = cash + sum(p["units"] * mark(sym, t_ms) for (sym, _), p in open_pos.items())
        equity_curve.append((ts, equity, cash))
    eq = pd.DataFrame(equity_curve, columns=["time", "equity", "cash"]).set_index("time")
    return {"equity": eq, "stats": stats, "log": log, "min_cash": float(eq["cash"].min())}


# ---------------------------------------------------------------- benchmarks (§9)

def bench_buy_hold(btc: pd.DataFrame, start: pd.Timestamp, med: float, m: float) -> pd.Series:
    d = btc[btc.index >= start]
    s, f = slippage(med) * m, FEE * m
    units = START_CAPITAL / (d["open"].iloc[0] * (1 + s) * (1 + f))
    eq = units * d["close"]
    eq.iloc[-1] = units * d["close"].iloc[-1] * (1 - s) * (1 - f)
    return eq


def bench_sma200(btc: pd.DataFrame, start: pd.Timestamp, med: float, m: float) -> tuple[pd.Series, int]:
    """Hold BTC from the next open after a daily close above SMA200; exit at the next open after a close at or
    below it (§9.3). Returns the equity curve and the number of round trips."""
    sma = btc["close"].rolling(200, min_periods=200).mean()
    s, f = slippage(med) * m, FEE * m
    d = btc[btc.index >= start]
    want = (btc["close"] > sma).shift(1).reindex(d.index).fillna(False).to_numpy()  # decided at previous close
    cash, units, trips, out = START_CAPITAL, 0.0, 0, []
    for k, (ts, row) in enumerate(d.iterrows()):
        if want[k] and units == 0:
            units = cash / (row["open"] * (1 + s) * (1 + f))
            cash = 0.0
        elif not want[k] and units > 0:
            cash = units * row["open"] * (1 - s) * (1 - f)
            units, trips = 0.0, trips + 1
        out.append(cash + units * row["close"])
    eq = pd.Series(out, index=d.index)
    if units > 0:
        eq.iloc[-1] = units * d["close"].iloc[-1] * (1 - s) * (1 - f)
    return eq, trips


def bench_equal_weight(raw_1d: dict[str, pd.DataFrame], membership: pd.DataFrame, med: pd.DataFrame,
                       start: pd.Timestamp, m: float) -> tuple[pd.Series, dict]:
    """Equal weight across the point-in-time universe, rebalanced at the first daily open of each month (§9.2).
    Costs on the traded amount per coin. One-pass cost approximation: the target value is reduced by the cost of
    reaching it. A coin whose data ends mid-month is sold at its last close."""
    opens = pd.DataFrame({s: d["open"] for s, d in raw_1d.items()})
    closes = pd.DataFrame({s: d["close"] for s, d in raw_1d.items()}).ffill()  # a missing day keeps the last close
    days = closes.index[closes.index >= start]
    last_day = {s: d.index[-1] for s, d in raw_1d.items()}
    hold: dict[str, float] = {}
    cash, out, info = START_CAPITAL, [], {"rebalances": 0, "delisted_sales": 0, "costs_usd": 0.0}
    prev_month = None
    for k, ts in enumerate(days):
        if ts.month != prev_month:
            prev_month = ts.month
            prev = days[k - 1] if k else ts - pd.Timedelta(days=1)
            members = [s for s in membership.columns if membership.at[prev, s]] if prev in membership.index else []
            members = [s for s in members if np.isfinite(opens.at[ts, s])]
            px = {s: opens.at[ts, s] if np.isfinite(opens.at[ts, s]) else closes.at[prev, s]
                  for s in set(members) | set(hold)}
            value = cash + sum(u * px[s] for s, u in hold.items())
            if members:
                tgt = value / len(members)
                cost = 0.0
                for s in set(members) | set(hold):
                    cur = hold.get(s, 0.0) * px[s]
                    want = tgt if s in members else 0.0
                    rate = (slippage(med.at[prev, s] if prev in med.index else np.nan) + FEE) * m
                    cost += abs(want - cur) * rate
                tgt = (value - cost) / len(members)
                hold = {s: tgt / px[s] for s in members}
                cash = 0.0
                info["rebalances"] += 1
                info["costs_usd"] += cost
        for s in [s for s in hold if last_day[s] <= ts]:  # data ends: sell at the last close
            rate = (slippage(med[s].dropna().iloc[-1] if med[s].notna().any() else np.nan) + FEE) * m
            cash += hold.pop(s) * closes.at[last_day[s], s] * (1 - rate)
            info["delisted_sales"] += 1
        out.append(cash + sum(u * closes.at[ts, s] for s, u in hold.items()))
    return pd.Series(out, index=days), info


# ---------------------------------------------------------------- metrics (§10)

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def bootstrap_ci(x: np.ndarray, n: int = 2000, seed: int = 5) -> tuple[float, float]:
    if len(x) < 2:
        return (math.nan, math.nan)
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def trade_metrics(trades: list[Trade], m: float) -> dict:
    r = np.array([t.r[m] for t in trades], dtype=float)
    n = len(r)
    if not n:
        return {"n": 0}
    wins = int((r > 0).sum())
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    kinds: dict[str, int] = {}
    for t in trades:
        kinds[t.exit_kind] = kinds.get(t.exit_kind, 0) + 1
    return {
        "n": n, "win_rate": wins / n, "win_ci": wilson(wins, n), "exp_r": float(r.mean()),
        "exp_ci": bootstrap_ci(r), "median_r": float(np.median(r)), "pf": float(pos / neg) if neg else math.inf,
        "worst_r": float(r.min()), "best_r": float(r.max()), "sum_r": float(r.sum()),
        "below_minus_1r": int((r < -1 - 1e-9).sum()), "avg_bars": float(np.mean([t.bars_held for t in trades])),
        "exits": kinds, "same_candle_stop_target": sum(t.same_candle_stop_target for t in trades),
        "high_risk": sum(t.high_risk for t in trades),
        "cost_r": float(np.mean([t.r[0.0] - t.r[m] for t in trades])),
        "r_hist": {b: int(((r >= lo) & (r < hi)).sum()) for b, lo, hi in
                   (("<-1", -np.inf, -1 - 1e-9), ("-1..0", -1 - 1e-9, 0), ("0..1", 0, 1), ("1..2", 1, 2),
                    ("2..5", 2, 5), (">=5", 5, np.inf))},
    }


def curve_metrics(eq: pd.Series) -> dict:
    """Daily-sampled equity: total return, CAGR, max drawdown and its length, volatility, Sharpe, Sortino."""
    daily = eq.groupby(eq.index.normalize()).last()
    ret = daily.pct_change().dropna()
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    peak = daily.cummax()
    dd = daily / peak - 1
    under = (dd < 0).astype(int)
    longest, run = 0, 0
    for u in under:
        run = run + 1 if u else 0
        longest = max(longest, run)
    downside = ret[ret < 0]
    return {
        "start": str(daily.index[0].date()), "end": str(daily.index[-1].date()),
        "final": float(daily.iloc[-1]), "total_return": float(daily.iloc[-1] / daily.iloc[0] - 1),
        "cagr": float((daily.iloc[-1] / daily.iloc[0]) ** (1 / years) - 1) if daily.iloc[-1] > 0 else -1.0,
        "max_dd": float(dd.min()), "longest_dd_days": int(longest),
        "vol": float(ret.std() * math.sqrt(365)) if len(ret) > 1 else math.nan,
        "sharpe": float(ret.mean() / ret.std() * math.sqrt(365)) if ret.std() > 0 else math.nan,
        "sortino": float(ret.mean() / downside.std() * math.sqrt(365)) if len(downside) > 1 and downside.std() > 0 else math.nan,
    }
