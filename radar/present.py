"""Turn an engine result row into the dict shown to the user (scan, Time Machine, site). One implementation."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .store import iso
from .strategies import SIGNAL, WATCH

PARTIAL_WINDOW = 20


def num(v):
    try:
        return None if v in (None, "") else float(v)
    except ValueError:
        return None


def clean(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if np.isnan(v) else round(float(v), 8)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def item(strategy, sym: str, interval: str, df: pd.DataFrame, ev: pd.DataFrame, i: int, snap: dict,
         high_risk_pct: float = 10.0) -> dict:
    row, crow = ev.iloc[i], df.iloc[i]
    stop, target, rr, rr_note = clean(row["stop"]), clean(row["target"]), clean(row["rr"]), None
    invalidation = invalidation_he = None
    if row["state"] == WATCH:
        # A WATCH is not an entry: no stop, target or R:R — only the level that cancels the setup.
        stop = target = rr = None
        rr_note = "יחושב רק לאיתות מאושר"
        invalidation, invalidation_he = strategy.watch_invalidation(row)
        invalidation = clean(invalidation)
    elif rr is None:
        rr_note = ("לא ניתן לחשב — אין יעד מוגדר באסטרטגיה (יציאה בסטופ נגרר)" if np.isnan(row["target"])
                   else "לא ניתן לחשב — היעד אינו מעל מחיר הייחוס")
    return {
        "symbol": sym, "base": sym[:-4], "timeframe": interval, "strategy": strategy.id,
        "strategy_version": strategy.version, "params_hash": strategy.params_hash, "state": row["state"],
        "candle_open_utc": iso(int(crow["open_time"])), "candle_close_utc": iso(int(crow["close_time"]) + 1),
        "ref_price": clean(crow["close"]), "stop": stop, "target": target, "rr": rr, "rr_note": rr_note,
        "risk_pct_to_stop": clean((crow["close"] - stop) / crow["close"] * 100) if stop is not None else None,
        # Presentation flag only: the stop is never tightened automatically (SPEC §3.0).
        "high_risk": bool(stop is not None and (crow["close"] - stop) / crow["close"] * 100 > high_risk_pct),
        "watch_invalidation": invalidation, "watch_invalidation_he": invalidation_he,
        "breakout_level": clean(row["breakout_level"]) if "breakout_level" in row else None,
        "atr_pct": clean(crow["atr_pct"]), "realized_vol_30": clean(crow["realized_vol_30"]),
        "history_candles": int(i + 1), "member": bool(ev["member"].iloc[i]) if "member" in ev else None,
        "partial_in_window": bool(df["partial"].iloc[max(0, i - PARTIAL_WINDOW + 1): i + 1].any()),
        "median_qvol_30d": num(snap.get("median_quote_volume_30d")), "spread_pct": num(snap.get("spread_pct")),
        "depth_bid_1pct_usd": num(snap.get("depth_bid_1pct_usd")), "depth_ask_1pct_usd": num(snap.get("depth_ask_1pct_usd")),
        "liquidity_warnings": [w for w in (snap.get("liquidity_warnings") or "").split(";") if w],
        "reason_he": strategy.reason_he(row) if row["state"] in (SIGNAL, WATCH) else None,
        "rules": [{k: clean(v) for k, v in r.items()} for r in strategy.explain(row)],
    }


