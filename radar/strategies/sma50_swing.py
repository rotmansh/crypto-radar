"""Strategy D — short swing on a pullback to SMA50 (RESEARCH; docs/STRATEGY_D_SPEC.md).

Research only: not part of the live scan or the dashboard until its results are approved. Strategy A is untouched.

The six versions share the same entry rules; they differ only in trade management after entry (break-even after
1R and/or an ATR trailing stop), which the backtest applies (radar.backtest.simulate_d). Every version still has
its own version number and params hash, so their results never mix.

Timing vs trend (SPEC §3): the trading chart's SMA50 times the pullback and the confirmation candle; the trend is
read from the next higher chart (1h -> 4h, 4h -> 1d), using only the higher candle that closed by the current
candle's close.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest import FEE, slippage
from .base import Rule, Strategy, flag, fmt, fmt_pct

HIGHER = {"1h": "4h", "4h": "1d"}
REVIEW_CANDLES = {"1h": 72, "4h": 18}  # 72 hours


def asof(frame: pd.DataFrame, other: pd.DataFrame | None, values: pd.Series | np.ndarray) -> np.ndarray:
    """Value of `values` (indexed like `other`) from the latest `other` candle closed by each `frame` candle's close."""
    if other is None or not len(other):
        return np.full(len(frame), np.nan)
    k = np.searchsorted(other["close_time"].to_numpy(), frame["close_time"].to_numpy(), side="right") - 1
    v = np.asarray(values, dtype=float)
    return np.where(k >= 0, v[np.clip(k, 0, None)], np.nan)


def respected_touches_atr(df: pd.DataFrame, band_atr: float, horizon: int, target_atr: float, break_atr: float,
                          merge_gap: int) -> pd.Series:
    """Like strategy A's respected touches, with ATR distances instead of percentages (SPEC D §5.2 S2).
    Touch: low <= SMA50 + band·ATR. Respected: within `horizon` candles high reaches SMA50 + target·ATR before any
    close below SMA50 − break·ATR. The value at i depends on rows up to i + horizon; it is only read after that."""
    low, high, close = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
    sma, atr = df["sma50"].to_numpy(), df["atr14"].to_numpy()
    n = len(df)
    out = np.full(n, np.nan)
    last = -10**9
    for i in range(n):
        if np.isnan(sma[i]) or np.isnan(atr[i]):
            continue
        if not low[i] <= sma[i] + band_atr * atr[i]:
            out[i] = 0.0
            continue
        new = i - last >= merge_gap
        last = i
        if not new:
            out[i] = 0.0
            continue
        val = 0.0
        for j in range(i + 1, min(n, i + horizon + 1)):
            if close[j] < sma[j] - break_atr * atr[j]:
                break
            if high[j] >= sma[j] + target_atr * atr[j]:
                val = 1.0
                break
        out[i] = val
    return pd.Series(out, index=df.index)


class SMA50Swing(Strategy):
    research = True
    id = "D-SMA50SWING"
    name_he = "D מחקר · מסחר קצר בתיקון ל-SMA50"
    timeframes = ("1h", "4h")
    min_candles = 220
    ENTRY = {
        "min_median_qvol_usd": 20_000_000, "htf_sma50_slope_lookback": 10, "sma50_slope_lookback": 10,
        "respect_window": 150, "respect_offset": 10, "respect_horizon": 10, "respect_band_atr": 0.25,
        "respect_target_atr": 1.0, "respect_break_atr": 1.0, "respect_merge_gap": 10, "min_respects": 2,
        "extension_lookback": 20, "extension_atr": 1.0, "pullback_lookback": 5, "touch_band_atr": 0.25,
        "max_depth_atr": 1.0, "close_floor_atr": 0.25, "watch_lookback": 3, "stop_atr": 0.2,
        "max_stop_atr": 3.0, "min_risk_cost_multiple": 4.0, "cooldown": 10,
    }
    MANAGEMENT = {  # version -> (break-even after 1R, trailing ATR multiple or None)  (SPEC D §7)
        "0.1.0": (True, None), "0.2.0": (True, 2.0), "0.3.0": (True, 3.0),
        "0.4.0": (False, None), "0.5.0": (False, 2.0), "0.6.0": (False, 3.0),
    }
    EXIT = {"review_hours": 72, "review_min_r": 0.5, "trend_exit_atr": 0.25, "trend_exit_closes": 2}

    watch_rules = ("liquid", "htf_trend", "htf_rising", "sma50_above_sma200", "sma50_rising", "prior_respects",
                   "extension", "touch", "not_too_deep", "no_close_break")
    signal_rules = watch_rules + ("watch_before", "green", "close_above_sma50", "close_above_prev_high", "complete",
                                  "stop_not_too_wide", "risk_vs_cost")

    rules = (
        Rule("liquid", "חציון מחזור 30 יום ≥ $20M", lambda r: f"חציון מחזור 30 יום ${fmt(r.median_qvol)}"),
        Rule("htf_trend", "מגמה בגרף הגבוה: סגירה > SMA50 > SMA200",
             lambda r: f"גרף גבוה: סגירה {fmt(r.htf_close)} · SMA50 {fmt(r.htf_sma50)} · SMA200 {fmt(r.htf_sma200)}"),
        Rule("htf_rising", "SMA50 בגרף הגבוה עולה", lambda r: f"SMA50 גבוה {fmt(r.htf_sma50)} מול {fmt(r.htf_sma50_prev)}"),
        Rule("sma50_above_sma200", "SMA50 > SMA200", lambda r: f"SMA50 {fmt(r.sma50)} · SMA200 {fmt(r.sma200)}"),
        Rule("sma50_rising", "SMA50 עולה (10 נרות)", lambda r: f"SMA50 {fmt(r.sma50)} מול {fmt(r.sma50_prev)}"),
        Rule("prior_respects", "לפחות 2 כיבודים קודמים של SMA50", lambda r: f"{fmt(r.respect_count)} כיבודים"),
        Rule("extension", "התרחקות של 1·ATR לפני התיקון",
             lambda r: f"שיא סגירה 20 נרות {fmt(r.ext_high)} מול {fmt(r.ext_level)}"),
        Rule("touch", "נגיעה באזור SMA50 (+0.25·ATR)", lambda r: f"שפל 5 נרות {fmt(r.low5)} מול {fmt(r.touch_level)}"),
        Rule("not_too_deep", "שפל לא עמוק מ-SMA50 − 1·ATR", lambda r: f"שפל {fmt(r.low5)} מול {fmt(r.depth_level)}"),
        Rule("no_close_break", "אין סגירה מתחת ל-SMA50 − 0.25·ATR",
             lambda r: f"סגירה נמוכה {fmt(r.close5)} מול {fmt(r.close_floor)}"),
        Rule("watch_before", "היה WATCH ב-3 הנרות הקודמים", lambda r: "כן" if r.c_watch_before == 1 else "לא"),
        Rule("green", "נר ירוק", lambda r: f"פתיחה {fmt(r.open)} · סגירה {fmt(r.close)}"),
        Rule("close_above_sma50", "סגירה מעל SMA50", lambda r: f"סגירה {fmt(r.close)} מול {fmt(r.sma50)}"),
        Rule("close_above_prev_high", "סגירה מעל שיא הנר הקודם", lambda r: f"סגירה {fmt(r.close)} מול {fmt(r.prev_high)}"),
        Rule("complete", "נר שלם", lambda r: "נר שלם" if r.c_complete == 1 else "נר חלקי"),
        Rule("stop_not_too_wide", "סטופ עד 3·ATR", lambda r: f"מרחק לסטופ {fmt_pct(r.stop_atr_dist)} ATR"),
        Rule("risk_vs_cost", "סיכון ≥ פי 4 מעלות הלוך-חזור", lambda r: f"פי {fmt_pct(r.risk_cost_ratio)} מהעלות"),
    )

    def __init__(self, version: str = "0.2.0"):
        be, trail = self.MANAGEMENT[version]
        self.version = version
        self.params = dict(self.ENTRY, **self.EXIT, break_even_after_1r=be, trail_atr=trail)
        self.cooldown = self.ENTRY["cooldown"]

    def watch_invalidation(self, row: pd.Series) -> tuple[float, str]:
        lvl = row.sma50 - self.ENTRY["close_floor_atr"] * row.atr14
        return lvl, f"סגירה מתחת ל-{fmt(lvl)} (SMA50 − 0.25·ATR) מבטלת את ההתראה"

    def conditions(self, df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        p = self.ENTRY
        series = ctx.get("series") or {}
        tf = ctx.get("timeframe") or ("1h" if len(df) > 1 and int(df["open_time"].iloc[1] - df["open_time"].iloc[0]) == 3_600_000 else "4h")
        htf = series.get(HIGHER[tf])
        daily = series.get("1d")
        c, o, h, lo = df["close"], df["open"], df["high"], df["low"]
        sma50, sma200, atr = df["sma50"], df["sma200"], df["atr14"]
        out = df[["open", "high", "low", "close", "sma50", "sma200", "atr14"]].copy()

        med = daily["quote_volume"].rolling(30, min_periods=30).median() if daily is not None else None
        out["median_qvol"] = asof(df, daily, med) if med is not None else np.nan
        out["c_liquid"] = flag(out["median_qvol"] >= p["min_median_qvol_usd"], out["median_qvol"])
        for col in ("close", "sma50", "sma200"):
            out[f"htf_{col}"] = asof(df, htf, htf[col]) if htf is not None else np.nan
        out["htf_sma50_prev"] = (asof(df, htf, htf["sma50"].shift(p["htf_sma50_slope_lookback"]))
                                 if htf is not None else np.nan)
        out["c_htf_trend"] = flag((out["htf_close"] > out["htf_sma50"]) & (out["htf_sma50"] > out["htf_sma200"]),
                                  out["htf_close"], out["htf_sma50"], out["htf_sma200"])
        out["c_htf_rising"] = flag(out["htf_sma50"] > out["htf_sma50_prev"], out["htf_sma50"], out["htf_sma50_prev"])

        out["sma50_prev"] = sma50.shift(p["sma50_slope_lookback"])
        out["c_sma50_above_sma200"] = flag(sma50 > sma200, sma50, sma200)
        out["c_sma50_rising"] = flag(sma50 > out["sma50_prev"], sma50, out["sma50_prev"])
        resp = respected_touches_atr(df, p["respect_band_atr"], p["respect_horizon"], p["respect_target_atr"],
                                     p["respect_break_atr"], p["respect_merge_gap"])
        out["respect_count"] = resp.shift(p["respect_offset"] + 1).rolling(p["respect_window"],
                                                                            min_periods=p["respect_window"]).sum()
        out["c_prior_respects"] = flag(out["respect_count"] >= p["min_respects"], out["respect_count"])
        out["ext_high"] = c.shift(1).rolling(p["extension_lookback"], min_periods=p["extension_lookback"]).max()
        out["ext_level"] = sma50 + p["extension_atr"] * atr
        out["c_extension"] = flag(out["ext_high"] >= out["ext_level"], out["ext_high"], out["ext_level"])

        n = p["pullback_lookback"]
        out["low5"] = lo.rolling(n, min_periods=n).min()
        out["close5"] = c.rolling(n, min_periods=n).min()
        out["touch_level"] = sma50 + p["touch_band_atr"] * atr
        out["depth_level"] = sma50 - p["max_depth_atr"] * atr
        out["close_floor"] = sma50 - p["close_floor_atr"] * atr
        out["c_touch"] = flag(out["low5"] <= out["touch_level"], out["low5"], out["touch_level"])
        out["c_not_too_deep"] = flag(out["low5"] >= out["depth_level"], out["low5"], out["depth_level"])
        out["c_no_close_break"] = flag(out["close5"] >= out["close_floor"], out["close5"], out["close_floor"])

        watch = out[[f"c_{k}" for k in self.watch_rules]]
        watch_all = (watch == 1).all(axis=1).astype(float)
        watch_all[watch.isna().any(axis=1)] = np.nan
        prior = watch_all.shift(1).rolling(p["watch_lookback"], min_periods=p["watch_lookback"]).max()
        out["c_watch_before"] = flag(prior == 1, prior)

        out["prev_high"] = h.shift(1)
        out["c_green"] = flag(c > o)
        out["c_close_above_sma50"] = flag(c > sma50, sma50)
        out["c_close_above_prev_high"] = flag(c > out["prev_high"], out["prev_high"])
        out["c_complete"] = flag(~df["partial"].astype(bool))

        out["stop"] = out["low5"] - p["stop_atr"] * atr
        out["target"] = np.nan  # no fixed target: break-even / trailing stop / 72h review / trend exit
        dist = c - out["stop"]
        out["stop_atr_dist"] = dist / atr
        out["c_stop_not_too_wide"] = flag(out["stop_atr_dist"] <= p["max_stop_atr"], out["stop_atr_dist"])
        slip = np.array([slippage(v) for v in out["median_qvol"].to_numpy()])
        round_trip = 2 * (FEE + slip) * c
        out["risk_cost_ratio"] = dist / round_trip
        out["c_risk_vs_cost"] = flag(out["risk_cost_ratio"] >= p["min_risk_cost_multiple"], out["risk_cost_ratio"])
        return out


VERSIONS = tuple(SMA50Swing.MANAGEMENT)
