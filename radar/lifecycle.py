"""Lifecycle of LIVE alerts (journal rows only): new, active or ended — and why it ended.

  WATCH episode   new     its first candle is the latest closed candle
                  active  still WATCH on the latest candle, began earlier
                  ended   the first later candle that is not WATCH: became a SIGNAL, closed below the level that cancels
                          the watch, left the universe, lost data, or the watch conditions stopped holding
  SIGNAL          new     confirmed on the latest closed candle
                  active  within its 10-candle window (the strategy's cooldown) and no close below its stop
                  ended   a close below the stop, or the window elapsed. This is status tracking only: no profit or loss
                          is computed here (phase 5).
  unknown         the alert cannot be followed (series stale or missing, strategy version changed); never guessed.

States come from the shared engine; nothing is re-implemented. Historical reconstructions never enter here.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .strategies import SIGNAL, WATCH

NOT_MEMBER = "NOT_IN_UNIVERSE"
REASONS_HE = {
    "became_signal": "הפכה לאיתות מאושר",
    "invalidated": "נסגרה מתחת לרמת הביטול",
    "left_universe": "המטבע יצא מהיקום",
    "insufficient": "חסרים נתונים להערכה",
    "conditions_ended": "תנאי המעקב חדלו להתקיים",
    "closed_below_stop": "נר נסגר מתחת לסטופ",
    "window_elapsed": "חלון 10 הנרות הסתיים",
    "not_followable": "לא ניתן לעקוב (נתונים לא עדכניים או גרסה אחרת)",
}


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def follow(row: dict, evals: dict, signal_window: int = 10) -> dict:
    """Status of one journal row. `evals[(strategy_id, timeframe, symbol)] = (strategy, df, ev)` from the engine."""
    out = {k: row.get(k) for k in ("signal_id", "origin", "symbol", "timeframe", "strategy", "strategy_version",
                                    "type", "ref_price", "stop", "target", "watch_invalidation", "breakout_level",
                                    "high_risk", "detected_at_utc")}
    ep_utc = row.get("episode_started_utc") or row.get("candle_open_utc")
    out["episode_started_utc"] = ep_utc
    key = (row["strategy"], row["timeframe"], row["symbol"])
    unknown = dict(out, status="unknown", reason="not_followable", ended_candle_utc=None, candles_since_start=None)
    if key not in evals:
        return unknown
    strategy, df, ev = evals[key]
    if strategy.version != row.get("strategy_version"):
        return unknown
    ep_ms = int(pd.Timestamp(ep_utc).value // 10**6)
    hits = np.flatnonzero(df["open_time"].to_numpy() == ep_ms)
    if not len(hits):
        return unknown
    i0, last = int(hits[0]), len(df) - 1
    states = ev["state"].to_numpy()
    close = df["close"].to_numpy()
    out["candles_since_start"] = last - i0
    end, reason = None, None
    if row["type"] == WATCH:
        for j in range(i0 + 1, last + 1):
            if states[j] != WATCH:
                end = j
                if states[j] == SIGNAL:
                    reason = "became_signal"
                elif states[j] == NOT_MEMBER:
                    reason = "left_universe"
                elif states[j] == "INSUFFICIENT":
                    reason = "insufficient"
                else:
                    level, _ = strategy.watch_invalidation(ev.iloc[j])
                    reason = "invalidated" if close[j] < level else "conditions_ended"
                break
    else:
        stop = _num(row.get("stop"))
        for j in range(i0 + 1, min(last, i0 + signal_window) + 1):
            if stop is not None and close[j] < stop:
                end, reason = j, "closed_below_stop"
                break
        if end is None and last - i0 >= signal_window:
            end, reason = i0 + signal_window, "window_elapsed"
    if end is not None:
        return dict(out, status="ended", reason=reason, ended_candle_utc=_iso(int(df["open_time"].iloc[end])))
    return dict(out, status="new" if i0 == last else "active", reason=None, ended_candle_utc=None)


def lifecycle(rows: list[dict], evals: dict, now: dt.datetime, recent_days: int = 7) -> list[dict]:
    """Follow every journal row; drop ended alerts that ended more than `recent_days` ago."""
    out = []
    cutoff = now - dt.timedelta(days=recent_days)
    for r in rows:
        f = follow(r, evals)
        if f["status"] == "ended" and dt.datetime.fromisoformat(f["ended_candle_utc"].replace("Z", "+00:00")) < cutoff:
            continue
        out.append(f)
    return out
