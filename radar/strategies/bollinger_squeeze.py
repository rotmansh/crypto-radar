"""Strategy B — Bollinger Squeeze + Breakout (SPEC §3.2). Long only."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Rule, Strategy, flag, fmt, fmt_pct


class BollingerSqueeze(Strategy):
    id = "B-BBSQ"
    version = "1.0.0"
    name_he = "B · התכנסות בולינגר ופריצה"
    timeframes = ("1d", "4h")
    params = {
        "squeeze_pct_max": 15, "watch_squeeze_lookback": 3, "signal_squeeze_lookback": 5,
        "range_lookback": 20, "volume_mult": 1.5,
    }
    min_candles = 200  # BB(20) + 180-candle percentile rank
    watch_rules = ("squeeze_recent", "above_mid", "below_level")
    signal_rules = ("squeeze_prior", "close_above_upper", "close_above_range", "volume_surge", "complete")

    rules = (
        Rule("squeeze_recent", "התכנסות ב-3 הנרות האחרונים",
             lambda r: f"אחוזון רוחב הרצועה הנמוך ב-3 נרות: {fmt_pct(r.bw_pct_min3)} (נדרש ≤ 15)"),
        Rule("above_mid", "סגירה מעל הרצועה האמצעית",
             lambda r: f"סגירה {fmt(r.close)} מול SMA20 {fmt(r.bb_mid)}"),
        Rule("below_level", "עדיין לא פרץ",
             lambda r: f"רמת פריצה {fmt(r.breakout_level)}, במרחק {fmt_pct(r.dist_to_level_pct)}% מהסגירה"),
        Rule("squeeze_prior", "התכנסות ב-5 הנרות שלפני הפריצה",
             lambda r: f"אחוזון רוחב הרצועה הנמוך ב-5 הנרות הקודמים: {fmt_pct(r.bw_pct_min_prior5)} (נדרש ≤ 15)"),
        Rule("close_above_upper", "סגירה מעל הרצועה העליונה",
             lambda r: f"סגירה {fmt(r.close)} מול רצועה עליונה {fmt(r.bb_upper)}"),
        Rule("close_above_range", "סגירה מעל שיא 20 הנרות",
             lambda r: f"סגירה {fmt(r.close)} מול שיא הטווח {fmt(r.range_high)}"),
        Rule("volume_surge", "נפח ≥ 1.5× הממוצע",
             lambda r: f"נפח פי {fmt_pct(r.volume_ratio)} מהממוצע של 20 הנרות הקודמים (נדרש ≥ 1.5)"),
        Rule("complete", "נר הפריצה שלם",
             lambda r: "נר שלם" if r.c_complete == 1 else "נר חלקי (עצירת מסחר) — לא משמש לאישור"),
    )

    def watch_invalidation(self, row: pd.Series) -> tuple[float, str]:
        return row.bb_mid, f"סגירה מתחת לרצועה האמצעית {fmt(row.bb_mid)} מבטלת את ההתראה"

    def conditions(self, df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        out = df[["open", "high", "low", "close", "volume", "bb_mid", "bb_upper", "bb_lower", "bb_width_pct",
                  "vol_sma20_prev"]].copy()
        squeeze = flag(df["bb_width_pct"] <= p["squeeze_pct_max"], df["bb_width_pct"])
        wl, sl, rl = p["watch_squeeze_lookback"], p["signal_squeeze_lookback"], p["range_lookback"]
        out["bw_pct_min3"] = df["bb_width_pct"].rolling(wl, min_periods=wl).min()
        out["bw_pct_min_prior5"] = df["bb_width_pct"].shift(1).rolling(sl, min_periods=sl).min()
        out["c_squeeze_recent"] = squeeze.rolling(wl, min_periods=wl).max()
        out["c_squeeze_prior"] = squeeze.shift(1).rolling(sl, min_periods=sl).max()

        out["range_high"] = df["high"].shift(1).rolling(rl, min_periods=rl).max()
        out["range_low"] = df["low"].shift(1).rolling(rl, min_periods=rl).min()
        out["breakout_level"] = np.fmax(df["bb_upper"], out["range_high"])
        out.loc[df["bb_upper"].isna() | out["range_high"].isna(), "breakout_level"] = np.nan
        out["dist_to_level_pct"] = (out["breakout_level"] / c - 1) * 100

        out["c_above_mid"] = flag(c > df["bb_mid"], df["bb_mid"])
        out["c_below_level"] = flag(c <= out["breakout_level"], out["breakout_level"])
        out["c_close_above_upper"] = flag(c > df["bb_upper"], df["bb_upper"])
        out["c_close_above_range"] = flag(c > out["range_high"], out["range_high"])
        out["volume_ratio"] = df["volume"] / df["vol_sma20_prev"]
        out["c_volume_surge"] = flag(out["volume_ratio"] >= p["volume_mult"], out["volume_ratio"])
        out["c_complete"] = flag(~df["partial"].astype(bool))

        out["stop"] = np.fmin(df["bb_mid"], df["low"])
        out.loc[df["bb_mid"].isna(), "stop"] = np.nan
        out["target"] = c + (out["range_high"] - out["range_low"])
        return out
