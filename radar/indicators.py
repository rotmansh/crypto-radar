"""Technical indicators as defined in SPEC §2. Every function is causal: the value at row t depends only on
rows <= t (verified by tests/test_indicators.py::test_no_lookahead)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .sources.base import Candle


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame([c.__dict__ for c in candles])
    if df.empty:
        return pd.DataFrame(columns=["open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume"])
    return df.set_index(pd.to_datetime(df["open_time"], unit="ms", utc=True), drop=False)


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    tr.iloc[:1] = (df["high"] - df["low"]).iloc[:1]  # first bar has no previous close
    return tr


def atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's ATR: seeded with the simple mean of the first n true ranges, then
    ATR_t = (ATR_{t-1}·(n−1) + TR_t) / n."""
    tr = true_range(df).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return pd.Series(out, index=df.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)  # population σ (SPEC §2)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + k * sd, "bb_lower": mid - k * sd})


def bandwidth(bb: pd.DataFrame) -> pd.Series:
    return (bb["bb_upper"] - bb["bb_lower"]) / bb["bb_mid"]


def percentile_rank(s: pd.Series, window: int = 180) -> pd.Series:
    """Share (0–100) of the last `window` values, current included, that are <= the current value."""
    return s.rolling(window, min_periods=window).apply(lambda w: (w <= w[-1]).mean() * 100, raw=True)


def volume_sma_prev(volume: pd.Series, n: int = 20) -> pd.Series:
    """Mean volume over candles t-n … t-1 (excludes the current candle)."""
    return volume.shift(1).rolling(n, min_periods=n).mean()


def realized_vol(close: pd.Series, n: int = 30, periods_per_year: int = 365) -> pd.Series:
    """Annualised stdev of log returns over n periods (sample σ)."""
    r = np.log(close / close.shift(1))
    return r.rolling(n, min_periods=n).std(ddof=1) * math.sqrt(periods_per_year)


def relative_strength(coin: pd.DataFrame, btc: pd.DataFrame) -> pd.Series:
    """close_coin / close_BTC on candles both series have (aligned by open time; no forward-filling)."""
    joined = coin[["close"]].join(btc[["close"]], how="inner", rsuffix="_btc")
    return joined["close"] / joined["close_btc"]


PERIODS_PER_YEAR = {"1d": 365, "4h": 365 * 6}


def compute_all(df: pd.DataFrame, interval: str = "1d") -> pd.DataFrame:
    out = df.copy()
    out["sma20"] = sma(df["close"], 20)
    out["sma50"] = sma(df["close"], 50)
    out["sma200"] = sma(df["close"], 200)
    out["atr14"] = atr_wilder(df, 14)
    out["atr_pct"] = out["atr14"] / df["close"] * 100
    out = out.join(bollinger(df["close"], 20, 2.0))
    out["bb_width"] = bandwidth(out)
    out["bb_width_pct"] = percentile_rank(out["bb_width"], 180)
    out["vol_sma20_prev"] = volume_sma_prev(df["volume"], 20)
    out["realized_vol_30"] = realized_vol(df["close"], 30, PERIODS_PER_YEAR[interval])
    return out
