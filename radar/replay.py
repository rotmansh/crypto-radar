"""Time Machine — Replay mode: what the strategies would have shown at `as_of`, from data available then only.

Replay runs the shared engine (radar.engine.run) on candles truncated at `as_of`; it has no strategy logic of its
own. Its output never contains a candle, price or state after `as_of` (tests/test_replay.py). Replays are stored
under data/replays/ and are never mixed into the live journal.

What a replay can and cannot know:
  - Candle availability = candle close time. Binance gives no "received at" timestamp, so the real arrival was a
    few seconds later; a replay exactly at a close instant is therefore slightly optimistic.
  - Universe = point-in-time rule (engine.pit_membership) applied to the coins we hold data for TODAY. Coins that
    were tradable then but have been delisted since are missing (survivorship bias). It is a historical
    reconstruction limited to available data, not a full market scan of that day.
  - Order-book liquidity, spreads and review decisions have no history; they are not used.
"""
from __future__ import annotations

import datetime as dt

from . import engine
from .present import clean, item
from .store import CandleStore, iso
from .strategies import ALL, INSUFFICIENT, SIGNAL, WATCH

LIMITS_HE = [
    "שחזור היסטורי מוגבל לנתונים הזמינים — לא סריקת שוק מלאה של אותו יום.",
    "הטיית שורדים: רשימת המועמדים כוללת רק מטבעות שיש לנו עליהם נתונים היום. מטבעות שנמחקו מאז חסרים.",
    "זמינות: נר נחשב זמין ברגע סגירתו. ל-Binance אין חותמת זמן של קבלת הנתון, ולכן בפועל הוא הגיע שניות אחר כך.",
    "נזילות ספר הפקודות, ספרד והחלטות בדיקה ידנית אינם זמינים היסטורית ואינם בשימוש בשחזור.",
    "מטבע שלא נסחר אז, או שאין לו מספיק היסטוריה, לא מקבל איתות.",
]


def parse_utc(s: str) -> int:
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp() * 1000)


def candidate_pool(store: CandleStore, exclusions: dict, review: dict) -> list[str]:
    """Coins we hold data for, minus explicit exclusions (asset type) and pairs on manual hold or under a documented
    suspension (order-book history does not exist, so the current decision is applied to the whole replay)."""
    holds = set(review.get("hold", {})) | set(review.get("suspended", {}))
    return [s for s in store.symbols("1d") if s[:-4] not in exclusions and s[:-4] not in holds]


def replay(store: CandleStore, settings: dict, exclusions: dict, review: dict, as_of_ms: int,
           timeframe: str = "1d", strategy_ids: tuple[str, ...] | None = None, code_commit: str = "") -> dict:
    strategies = tuple(s for s in ALL if strategy_ids is None or s.id in strategy_ids)
    pool = candidate_pool(store, exclusions, review)
    tfs = sorted({"1d", timeframe})
    raw = {tf: engine.load_candles(store, pool, tf, as_of_ms) for tf in tfs}
    res = engine.run(raw, settings["universe"], set(pool), strategies, as_of_ms=as_of_ms)

    span = engine.indicators.INTERVAL_MS[timeframe]
    expected_last = (as_of_ms // span) * span - span  # newest candle that had closed by as_of
    frames = res.frames.get(timeframe, {})
    newest = max((int(f["open_time"].iloc[-1]) for f in frames.values()), default=None)
    members_today = res.membership.iloc[-1] if not res.membership.empty else None
    universe = sorted(s for s, v in members_today.items() if v) if members_today is not None else []

    blocks = []
    for strat in strategies:
        if timeframe not in strat.timeframes:
            blocks.append({"strategy": strat.id, "version": strat.version, "params_hash": strat.params_hash,
                           "name_he": strat.name_he, "timeframe": timeframe, "available": False,
                           "note_he": f"האסטרטגיה פועלת רק בטווח {', '.join(strat.timeframes)}"})
            continue
        blk = {"strategy": strat.id, "version": strat.version, "params_hash": strat.params_hash,
               "name_he": strat.name_he, "timeframe": timeframe, "available": True,
               "signals": [], "watch": [], "insufficient": [], "stale": []}
        for sym, ev in sorted(res.evaluated[(strat.id, timeframe)].items()):
            df = frames[sym]
            last = len(ev) - 1
            if int(df["open_time"].iloc[last]) != expected_last:
                blk["stale"].append({"symbol": sym, "last_candle_utc": iso(int(df["open_time"].iloc[last]))})
                continue  # the coin had no candle closing at as_of (not listed yet, halted, or delisted)
            st = ev["state"].iloc[last]
            if st in (SIGNAL, WATCH):
                it = item(strat, sym, timeframe, df, ev, last, {})
                it["episode_started_utc"] = iso(int(df["open_time"].iloc[engine.episode_start(ev["state"], last)]))
                blk["signals" if st == SIGNAL else "watch"].append(it)
            elif st == INSUFFICIENT:
                blk["insufficient"].append({"symbol": sym, "history_candles": len(df), "needs": strat.min_candles})
        blocks.append(blk)

    return {
        "origin": "replay", "as_of_utc": iso(as_of_ms), "timeframe": timeframe,
        "last_available_candle_utc": iso(expected_last), "newest_candle_in_data_utc": iso(newest) if newest else None,
        "data_covers_as_of": newest == expected_last,
        "strategies": [s.ref for s in strategies], "params_hashes": {s.ref: s.params_hash for s in strategies},
        "data_fingerprint": res.data_fingerprint, "code_commit": code_commit,
        "candidate_pool_size": len(pool), "universe_at_as_of": universe,
        "results": blocks, "limits_he": LIMITS_HE,
        "reveal": {"available": False, "note_he": "חישוב תוצאות עדיין לא זמין — יחובר למנוע הבדיקות ההיסטוריות בשלב 5."},
    }
