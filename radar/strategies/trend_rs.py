"""Strategy C — Trend & Relative Strength vs BTC (SPEC §3.3). Daily only; BTC itself is not evaluated."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import sma
from .base import Rule, Strategy, flag, fmt, fmt_pct

RS_SMA, RS_RET, MIN_CROSS_SECTION = 50, 30, 10


def rs_panel(frames: dict[str, pd.DataFrame], btc: pd.DataFrame,
             membership: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Per symbol: RS line, its SMA50, 30-candle RS return, and the cross-sectional percentile of that return.

    The percentile on date d uses only coins that were universe members on d (`membership`, point-in-time; None =
    every coin given) and have a valid 30-candle RS return on d. Delisted coins are absent (see the survivorship
    note in every report). Fewer than MIN_CROSS_SECTION coins on a date leaves the rank undefined (NaN).
    """
    btc_close = btc["close"]
    per: dict[str, pd.DataFrame] = {}
    for sym, df in frames.items():
        rs = df["close"] / btc_close.reindex(df.index)  # no forward-fill: missing BTC candle -> NaN
        out = pd.DataFrame({"rs": rs}, index=df.index)
        out["rs_sma50"] = sma(rs, RS_SMA)
        out["rs_ret30"] = rs / rs.shift(RS_RET) - 1
        per[sym] = out
    if not per:
        return per
    wide = pd.DataFrame({s: d["rs_ret30"] for s, d in per.items()})
    if membership is not None:
        mask = membership.reindex(index=wide.index, columns=wide.columns).fillna(False).astype(bool)
        wide = wide.where(mask)
    n = wide.notna().sum(axis=1)
    pct = wide.rank(axis=1, method="max").div(n, axis=0) * 100  # share of coins with return <= this coin's
    pct[n < MIN_CROSS_SECTION] = np.nan
    for s, d in per.items():
        d["rs_rank"] = pct[s].reindex(d.index)
        d["rs_universe_n"] = n.reindex(d.index)
    return per


class TrendRelativeStrength(Strategy):
    id = "C-TRS"
    version = "1.1.0"  # 1.1.0: rank population is the point-in-time universe of each date (engine.pit_membership)
    name_he = "C · מגמה וחוזק יחסי מול BTC"
    timeframes = ("1d",)
    params = {
        "sma50_slope_lookback": 10, "rs_sma": RS_SMA, "rs_return_lookback": RS_RET, "rs_rank_min": 80,
        "min_cross_section": MIN_CROSS_SECTION, "new_high_lookback": 20, "stop_atr": 2.5,
    }
    min_candles = 200
    watch_rules = ("trend_stack", "sma50_rising", "rs_above_sma", "rs_ret_positive", "rs_rank", "no_new_high")
    core_rules = ("trend_stack", "sma50_rising", "rs_above_sma", "rs_ret_positive", "rs_rank", "new_high")
    signal_rules = core_rules + ("transition", "complete")

    rules = (
        Rule("trend_stack", "סגירה > SMA50 > SMA200",
             lambda r: f"סגירה {fmt(r.close)} · SMA50 {fmt(r.sma50)} · SMA200 {fmt(r.sma200)}"),
        Rule("sma50_rising", "SMA50 עולה (10 נרות)",
             lambda r: f"SMA50 עכשיו {fmt(r.sma50)} מול {fmt(r.sma50_prev)} לפני 10 נרות"),
        Rule("rs_above_sma", "קו RS מעל הממוצע שלו",
             lambda r: f"קו RS (מחיר/BTC) {fmt_pct((r.rs / r.rs_sma50 - 1) * 100)}% ביחס לממוצע 50 שלו"),
        Rule("rs_ret_positive", "הכה את BTC ב-30 יום",
             lambda r: f"תשואה יחסית ל-BTC ב-30 יום: {fmt_pct(r.rs_ret30_pct)}%"),
        Rule("rs_rank", "בין 20% החזקים ביקום",
             lambda r: f"אחוזון {fmt_pct(r.rs_rank)} מתוך {fmt(r.rs_universe_n)} מטבעות (נדרש ≥ 80)"),
        Rule("no_new_high", "בהתבססות (אין שיא 20 יום)",
             lambda r: f"סגירה {fmt(r.close)} מול שיא סגירה 20 יום {fmt(r.high20)}"),
        Rule("new_high", "סגירה בשיא 20 יום",
             lambda r: f"סגירה {fmt(r.close)} מול שיא סגירה 20 יום {fmt(r.high20)}"),
        Rule("transition", "מעבר ראשון למצב מתקיים",
             lambda r: "התנאים לא התקיימו בנר הקודם" if r.c_transition == 1 else "התנאים כבר התקיימו בנר הקודם"),
        Rule("complete", "נר האישור שלם",
             lambda r: "נר שלם" if r.c_complete == 1 else "נר חלקי (עצירת מסחר) — לא משמש לאישור"),
    )

    def watch_invalidation(self, row: pd.Series) -> tuple[float, str]:
        return row.sma50, f"סגירה מתחת ל-SMA50 {fmt(row.sma50)} מבטלת את ההתראה"

    def conditions(self, df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        p = self.params
        c, sma50, sma200 = df["close"], df["sma50"], df["sma200"]
        out = df[["open", "high", "low", "close", "sma50", "sma200", "atr14"]].copy()
        rs = ctx.get("rs")
        if rs is None:
            rs = pd.DataFrame(np.nan, index=df.index, columns=["rs", "rs_sma50", "rs_ret30", "rs_rank", "rs_universe_n"])
        rs = rs.reindex(df.index)
        for col in ("rs", "rs_sma50", "rs_ret30", "rs_rank", "rs_universe_n"):
            out[col] = rs[col]
        out["rs_ret30_pct"] = out["rs_ret30"] * 100
        out["sma50_prev"] = sma50.shift(p["sma50_slope_lookback"])
        out["c_trend_stack"] = flag((c > sma50) & (sma50 > sma200), sma50, sma200)
        out["c_sma50_rising"] = flag(sma50 > out["sma50_prev"], sma50, out["sma50_prev"])
        out["c_rs_above_sma"] = flag(out["rs"] > out["rs_sma50"], out["rs"], out["rs_sma50"])
        out["c_rs_ret_positive"] = flag(out["rs_ret30"] > 0, out["rs_ret30"])
        out["c_rs_rank"] = flag(out["rs_rank"] >= p["rs_rank_min"], out["rs_rank"])
        nh = p["new_high_lookback"]
        out["high20"] = c.shift(1).rolling(nh, min_periods=nh).max()
        out["c_new_high"] = flag(c > out["high20"], out["high20"])
        out["c_no_new_high"] = flag(c <= out["high20"], out["high20"])
        core = out[[f"c_{k}" for k in self.core_rules]]
        core_all = (core == 1).all(axis=1).astype(float)
        core_all[core.isna().any(axis=1)] = np.nan
        prev = core_all.shift(1)
        out["c_transition"] = flag(prev == 0, prev)
        out["c_complete"] = flag(~df["partial"].astype(bool))
        out["stop"] = c - p["stop_atr"] * df["atr14"]
        out["target"] = np.nan  # trend strategy: exit by trailing stop, no fixed target
        return out
