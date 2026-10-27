"""Strategy A — SMA 50 Pullback (inspired by the user's stock course; SPEC §3.1).

These numeric rules are a proposed translation of the course's general principles. They are NOT the
instructor's original rules.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Rule, Strategy, flag, fmt, fmt_pct


def respected_touches(df: pd.DataFrame, band: float, horizon: int, target_atr: float, break_level: float,
                      merge_gap: int) -> pd.Series:
    """1.0 at the first candle of each SMA50 touch episode that was "respected", 0.0 elsewhere, NaN without data.

    Touch: low <= SMA50 × (1 + band). Touches less than `merge_gap` candles apart form one episode.
    Respected: within the next `horizon` candles, high reaches SMA50 + target_atr·ATR14 before any close
    below SMA50 × break_level. The value at i depends on rows up to i + horizon; callers only read it after
    that (see `prior_respects`), so no future data reaches a decision.
    """
    low, high, close = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
    sma, atr = df["sma50"].to_numpy(), df["atr14"].to_numpy()
    n = len(df)
    touch = low <= sma * (1 + band)
    out = np.full(n, np.nan)
    last_touch = -10**9
    for i in range(n):
        if np.isnan(sma[i]) or np.isnan(atr[i]):
            continue
        if not touch[i]:
            out[i] = 0.0
            continue
        new_episode = i - last_touch >= merge_gap
        last_touch = i
        if not new_episode:
            out[i] = 0.0
            continue
        val = 0.0
        for j in range(i + 1, min(n, i + horizon + 1)):
            if close[j] < sma[j] * break_level:
                break
            if high[j] >= sma[j] + target_atr * atr[j]:
                val = 1.0
                break
        out[i] = val
    return pd.Series(out, index=df.index)


class SMA50Pullback(Strategy):
    id = "A-SMA50PB"
    version = "1.0.0"
    name_he = "A · תיקון לממוצע 50 (בהשראת הקורס)"
    timeframes = ("1d", "4h")
    params = {
        "sma200_slope_lookback": 20, "sma50_slope_lookback": 10,
        "respect_window": 150, "respect_offset": 10, "respect_horizon": 10, "respect_target_atr": 1.0,
        "respect_break_level": 0.97, "respect_merge_gap": 10, "min_respects": 2,
        "extension_lookback": 20, "extension_atr": 1.0,
        "pullback_lookback": 5, "touch_band": 0.01, "close_floor": 0.98,
        "stop_atr": 0.2, "target_from": 20, "target_to": 5,
    }
    min_candles = 220
    watch_rules = ("close_above_sma200", "sma50_above_sma200", "sma200_rising", "sma50_rising",
                   "prior_respects", "extension", "touch_recent", "no_close_break")
    signal_rules = watch_rules + ("green", "close_above_sma50", "close_above_prev_high", "complete")

    rules = (
        Rule("close_above_sma200", "סגירה מעל SMA200",
             lambda r: f"סגירה {fmt(r.close)} {'>' if r.c_close_above_sma200 == 1 else '≤'} SMA200 {fmt(r.sma200)}"),
        Rule("sma50_above_sma200", "SMA50 מעל SMA200",
             lambda r: f"SMA50 {fmt(r.sma50)} {'>' if r.c_sma50_above_sma200 == 1 else '≤'} SMA200 {fmt(r.sma200)}"),
        Rule("sma200_rising", "SMA200 עולה (20 נרות)",
             lambda r: f"SMA200 עכשיו {fmt(r.sma200)} מול {fmt(r.sma200_prev)} לפני 20 נרות"),
        Rule("sma50_rising", "SMA50 עולה (10 נרות)",
             lambda r: f"SMA50 עכשיו {fmt(r.sma50)} מול {fmt(r.sma50_prev)} לפני 10 נרות"),
        Rule("prior_respects", "כיבד את SMA50 בעבר (≥2)",
             lambda r: f"{'—' if pd.isna(r.respect_count) else int(r.respect_count)} כיבודים קודמים של SMA50 ב-150 נרות (נדרשים 2)"),
        Rule("extension", "התרחקות לפני התיקון (≥1 ATR)",
             lambda r: f"שיא סגירה ב-20 נרות {fmt(r.ext_high)} מול SMA50+ATR {fmt(r.ext_level)}"),
        Rule("touch_recent", "נגיעה בממוצע ב-5 נרות",
             lambda r: f"השפל הנמוך ב-5 נרות: {fmt_pct(r.touch_ratio_pct)}% מ-SMA50 (נדרש ≤ +1%)"),
        Rule("no_close_break", "אין סגירה מתחת ל-SMA50×0.98",
             lambda r: f"הסגירה הנמוכה ב-5 נרות: {fmt_pct(r.close_ratio_pct)}% מ-SMA50 (נדרש ≥ −2%)"),
        Rule("green", "נר ירוק",
             lambda r: f"פתיחה {fmt(r.open)} → סגירה {fmt(r.close)}"),
        Rule("close_above_sma50", "סגירה מעל SMA50",
             lambda r: f"סגירה {fmt(r.close)} מול SMA50 {fmt(r.sma50)}"),
        Rule("close_above_prev_high", "סגירה מעל שיא הנר הקודם",
             lambda r: f"סגירה {fmt(r.close)} מול שיא קודם {fmt(r.prev_high)}"),
        Rule("complete", "נר האישור שלם",
             lambda r: "נר שלם" if r.c_complete == 1 else "נר חלקי (עצירת מסחר) — לא משמש לאישור"),
    )

    def watch_invalidation(self, row: pd.Series) -> tuple[float, str]:
        level = row.sma50 * self.params["close_floor"]
        return level, f"סגירה מתחת ל-{fmt(level)} (SMA50×0.98) מבטלת את ההתראה"

    def conditions(self, df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        p = self.params
        c, o, h, lo = df["close"], df["open"], df["high"], df["low"]
        sma50, sma200, atr = df["sma50"], df["sma200"], df["atr14"]
        out = df[["open", "high", "low", "close", "sma50", "sma200", "atr14"]].copy()
        out["sma200_prev"] = sma200.shift(p["sma200_slope_lookback"])
        out["sma50_prev"] = sma50.shift(p["sma50_slope_lookback"])
        out["c_close_above_sma200"] = flag(c > sma200, sma200)
        out["c_sma50_above_sma200"] = flag(sma50 > sma200, sma50, sma200)
        out["c_sma200_rising"] = flag(sma200 > out["sma200_prev"], sma200, out["sma200_prev"])
        out["c_sma50_rising"] = flag(sma50 > out["sma50_prev"], sma50, out["sma50_prev"])

        resp = respected_touches(df, p["touch_band"], p["respect_horizon"], p["respect_target_atr"],
                                 p["respect_break_level"], p["respect_merge_gap"])
        # Episodes starting in [t-160, t-11]; each is resolved by t-1 at the latest.
        out["respect_count"] = resp.shift(p["respect_offset"] + 1).rolling(p["respect_window"],
                                                                            min_periods=p["respect_window"]).sum()
        out["c_prior_respects"] = flag(out["respect_count"] >= p["min_respects"], out["respect_count"])

        out["ext_high"] = c.shift(1).rolling(p["extension_lookback"], min_periods=p["extension_lookback"]).max()
        out["ext_level"] = sma50 + p["extension_atr"] * atr
        out["c_extension"] = flag(out["ext_high"] >= out["ext_level"], out["ext_high"], out["ext_level"])

        n = p["pullback_lookback"]
        low_ratio = lo / sma50 - 1
        close_ratio = c / sma50 - 1
        out["touch_ratio_pct"] = low_ratio.rolling(n, min_periods=n).min() * 100
        out["close_ratio_pct"] = close_ratio.rolling(n, min_periods=n).min() * 100
        out["c_touch_recent"] = flag(out["touch_ratio_pct"] <= p["touch_band"] * 100, out["touch_ratio_pct"])
        out["c_no_close_break"] = flag(out["close_ratio_pct"] >= (p["close_floor"] - 1) * 100, out["close_ratio_pct"])

        out["prev_high"] = h.shift(1)
        out["c_green"] = flag(c > o)
        out["c_close_above_sma50"] = flag(c > sma50, sma50)
        out["c_close_above_prev_high"] = flag(c > out["prev_high"], out["prev_high"])
        out["c_complete"] = flag(~df["partial"].astype(bool))

        out["stop"] = lo.rolling(n, min_periods=n).min() - p["stop_atr"] * atr
        span = p["target_from"] - p["target_to"] + 1
        out["target"] = h.shift(p["target_to"]).rolling(span, min_periods=span).max()
        return out
