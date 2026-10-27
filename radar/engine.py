"""The one strategy engine shared by the live scan, the Time Machine and (phase 5) the backtest.

    load_candles(store, symbols, interval, as_of_ms)   closed candles available at `as_of_ms`
    pit_membership(daily_frames, ...)                   point-in-time universe from data available on each date
    run(raw_by_tf, membership, strategies, ...)         indicators + strategy states for every candle

Availability: Binance klines carry the candle's open/close times but no "received at" timestamp. A candle is
treated as available from the moment it closes (close_time + 1 ms). That is the earliest it could have been
known; in reality it arrived seconds later. This limitation is stated wherever replays are shown.

Point-in-time universe: on each date, a coin is a member if — using only daily candles closed by then — it has at
least `lookback_days` candles and its median daily quote volume over the last `lookback_days` is at or above the
floor, ranked and capped at `max_size`. The candidate pool can only be coins we hold data for today, so coins that
were delisted before data collection began are missing (survivorship bias, stated in every output).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators
from .sources.base import Candle
from .store import CandleStore
from .strategies import NONE, Strategy, rs_panel

DAY_MS = 86_400_000
NOT_MEMBER = "NOT_IN_UNIVERSE"


def available(candles: list[Candle], as_of_ms: int | None) -> list[Candle]:
    """Candles that had closed by `as_of_ms` (close_time + 1 <= as_of)."""
    if as_of_ms is None:
        return candles
    return [c for c in candles if c.close_time < as_of_ms]


def load_candles(store: CandleStore, symbols: list[str], interval: str, as_of_ms: int | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        c = available(store.load(s, interval), as_of_ms)
        if c:
            out[s] = indicators.to_frame(c)
    return out


def pit_membership(daily: dict[str, pd.DataFrame], floor: float, lookback_days: int, max_size: int) -> pd.DataFrame:
    """Boolean frame (daily open time × symbol): member of the universe as of that day's close."""
    if not daily:
        return pd.DataFrame()
    med = pd.DataFrame({s: df["quote_volume"].rolling(lookback_days, min_periods=lookback_days).median()
                        for s, df in daily.items()})
    eligible = med >= floor
    rank = med.where(eligible).rank(axis=1, ascending=False, method="first")
    return eligible & (rank <= max_size)


def membership_for(frame: pd.DataFrame, membership: pd.DataFrame, symbol: str) -> pd.Series:
    """Membership aligned to any timeframe: each candle uses the latest daily candle closed by its own close."""
    if membership.empty or symbol not in membership.columns:
        return pd.Series(False, index=frame.index)
    day_open_ms = ((frame["close_time"].to_numpy() + 1) // DAY_MS - 1) * DAY_MS
    idx = pd.to_datetime(day_open_ms, unit="ms", utc=True)
    col = membership[symbol]
    return pd.Series(col.reindex(idx).fillna(False).to_numpy().astype(bool), index=frame.index)


@dataclass
class EngineResult:
    as_of_ms: int | None
    evaluated: dict[tuple[str, str], dict[str, pd.DataFrame]] = field(default_factory=dict)  # (strategy, tf) -> sym -> frame
    frames: dict[str, dict[str, pd.DataFrame]] = field(default_factory=dict)                  # tf -> sym -> indicator frame
    data_fingerprint: str = ""
    membership: pd.DataFrame = field(default_factory=pd.DataFrame)


def fingerprint(raw_by_tf: dict[str, dict[str, pd.DataFrame]]) -> str:
    """Identifies the exact input candles: per series count, last open time and checksum of OHLCV."""
    h = hashlib.sha256()
    for tf in sorted(raw_by_tf):
        for s in sorted(raw_by_tf[tf]):
            df = raw_by_tf[tf][s]
            arr = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
            h.update(f"{tf}|{s}|{len(df)}|{int(df['open_time'].iloc[-1])}|".encode())
            h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()[:16]


def run(raw_by_tf: dict[str, dict[str, pd.DataFrame]], universe_cfg: dict, candidates: set[str],
        strategies: tuple[Strategy, ...], as_of_ms: int | None = None, btc: str = "BTCUSDT") -> EngineResult:
    """Evaluate every strategy on every candle, using only candles closed by `as_of_ms` (None = all stored).

    `raw_by_tf` holds raw candle frames (indicators.to_frame); they are truncated here before any indicator or the
    universe is computed, so a replay never sees a later candle even indirectly. Membership comes from
    pit_membership on the truncated daily candles, restricted to `candidates`. A candle whose coin was not a member
    at that time gets state NOT_IN_UNIVERSE (its rules are still computed for transparency).
    """
    res = EngineResult(as_of_ms)
    trunc = {tf: {s: (df[df["close_time"] < as_of_ms] if as_of_ms is not None else df) for s, df in d.items()}
             for tf, d in raw_by_tf.items()}
    trunc = {tf: {s: df for s, df in d.items() if len(df)} for tf, d in trunc.items()}
    res.data_fingerprint = fingerprint(trunc)
    res.frames = {tf: {s: indicators.compute_all(df, tf) for s, df in d.items()} for tf, d in trunc.items()}
    raw_daily = {s: df for s, df in trunc.get("1d", {}).items() if s in candidates}
    membership = pit_membership(raw_daily, universe_cfg["min_median_quote_volume_usd"],
                                universe_cfg["volume_lookback_days"], universe_cfg["max_size"])
    res.membership = membership

    daily = res.frames.get("1d", {})
    panel = {}
    if btc in daily:
        members_daily = {s: f for s, f in daily.items() if s != btc}
        panel = rs_panel(members_daily, daily[btc], membership=membership)

    for strat in strategies:
        for tf in strat.timeframes:
            out = {}
            for s, f in res.frames.get(tf, {}).items():
                if strat.id.startswith("C-") and s == btc:
                    continue
                ev = strat.evaluate(f, {"rs": panel.get(s)})
                member = membership_for(f, membership, s)
                ev["member"] = member.to_numpy()
                ev.loc[~member.to_numpy(), "state"] = NOT_MEMBER
                out[s] = ev
            res.evaluated[(strat.id, tf)] = out
    return res


def episode_start(states: pd.Series, i: int) -> int:
    """Index where the run of identical states ending at i began (a WATCH episode's first candle)."""
    st = states.to_numpy()
    j = i
    while j > 0 and st[j - 1] == st[i]:
        j -= 1
    return j
