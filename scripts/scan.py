"""Run the three strategies on the latest closed candles of the current universe.

Writes:
  data/scan/<tag>.json           full scan result of this run (new file per run)
  data/scan/latest.json          the same, as the current state for the dashboard (overwritten each run)
  journal/signals.csv            append-only: every confirmed SIGNAL, and every WATCH on the candle it starts
  journal/runs.csv               append-only: one row per scan
  reports/phase2/runs/<tag>/     REPORT.md (Hebrew) + historical_signals.csv — where the rules fired in the
                                 stored history. Occurrences only: no performance is computed or implied.

Uses only stored closed candles. A symbol whose latest stored candle is not the most recently closed one is
reported as stale and not evaluated. No trading, no account access.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from radar import config, indicators  # noqa: E402
from radar.store import CandleStore, iso  # noqa: E402
from radar.strategies import ALL, INSUFFICIENT, SIGNAL, WATCH, rs_panel  # noqa: E402
from radar.strategies.base import fmt  # noqa: E402

SURVIVORSHIP_HE = ("**הטיית שורדים:** הנתונים ההיסטוריים כוללים רק מטבעות שנסחרים היום ב-Binance וברשימה הנוכחית. "
                   "מטבעות שנמחקו או שירדו מהרשימה בעבר חסרים, ולכן כל ספירה או תוצאה היסטורית עלולה להיות "
                   "אופטימית מהמציאות. תיעוד מתוארך של הרשימה קיים רק מ-27.10.2026.")
NOT_PROOF_HE = ("**אין כאן הוכחה לרווחיות.** הדוח מציג היכן הכללים התקיימו בעבר, ללא חישוב ביצועים. "
                "גם בדיקה היסטורית עתידית (שלב 5) לא תהיה הוכחה לתוצאות עתידיות.")
JOURNAL_FIELDS = ["signal_id", "detected_at_utc", "candle_open_utc", "candle_close_utc", "exchange", "symbol",
                  "timeframe", "strategy", "strategy_version", "params_hash", "type", "ref_price", "stop", "target",
                  "rr", "atr_pct", "realized_vol_30", "median_qvol_30d", "spread_pct", "liquidity_warnings",
                  "partial_in_window", "reason_he", "code_commit"]
PARTIAL_WINDOW = 20


def code_commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:12]
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def latest_snapshot() -> tuple[Path, list[dict]]:
    path = sorted((ROOT / "data/universe/snapshots").glob("*.csv"))[-1]
    with path.open(newline="") as f:
        return path, list(csv.DictReader(f))


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


def item(strategy, sym: str, interval: str, df: pd.DataFrame, ev: pd.DataFrame, i: int, snap: dict) -> dict:
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
        "watch_invalidation": invalidation, "watch_invalidation_he": invalidation_he,
        "breakout_level": clean(row["breakout_level"]) if "breakout_level" in row else None,
        "atr_pct": clean(crow["atr_pct"]), "realized_vol_30": clean(crow["realized_vol_30"]),
        "history_candles": int(i + 1),
        "partial_in_window": bool(df["partial"].iloc[max(0, i - PARTIAL_WINDOW + 1): i + 1].any()),
        "median_qvol_30d": num(snap.get("median_quote_volume_30d")), "spread_pct": num(snap.get("spread_pct")),
        "depth_bid_1pct_usd": num(snap.get("depth_bid_1pct_usd")), "depth_ask_1pct_usd": num(snap.get("depth_ask_1pct_usd")),
        "liquidity_warnings": [w for w in (snap.get("liquidity_warnings") or "").split(";") if w],
        "reason_he": strategy.reason_he(row) if row["state"] in (SIGNAL, WATCH) else None,
        "rules": [{k: clean(v) for k, v in r.items()} for r in strategy.explain(row)],
    }


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    now_ms = int(started.timestamp() * 1000)
    run_id = os.environ.get("GITHUB_RUN_ID")
    tag = f"{started:%Y-%m-%d}-run-{run_id}" if run_id else f"{started:%Y-%m-%d}-local-{started:%H%M%S}"
    settings = config.load_settings()
    store = CandleStore(ROOT / settings["data"]["store_dir"], settings["data"]["source"])
    snap_path, snap_rows = latest_snapshot()
    snap = {r["symbol"]: r for r in snap_rows}
    universe = sorted(r["symbol"] for r in snap_rows if r["status"] == "included")
    commit = code_commit()

    frames: dict[str, dict[str, pd.DataFrame]] = defaultdict(dict)
    stale: list[dict] = []
    for interval in settings["data"]["intervals"]:
        span = indicators.INTERVAL_MS[interval]
        expected_last_open = (now_ms // span) * span - span
        for sym in sorted(set(universe) | {"BTCUSDT"}):
            candles = store.load(sym, interval)
            if not candles or candles[-1].open_time != expected_last_open:
                stale.append({"symbol": sym, "timeframe": interval,
                              "last_stored": iso(candles[-1].open_time) if candles else None,
                              "expected": iso(expected_last_open)})
                continue
            frames[interval][sym] = indicators.compute_all(indicators.to_frame(candles), interval)

    daily = frames.get("1d", {})
    btc_daily = daily.get("BTCUSDT")
    panel = rs_panel({s: f for s, f in daily.items() if s != "BTCUSDT" and s in universe}, btc_daily) \
        if btc_daily is not None else {}
    btc_regime = None
    if btc_daily is not None and not np.isnan(btc_daily["sma200"].iloc[-1]):
        b = btc_daily.iloc[-1]
        btc_regime = {"candle_open_utc": iso(int(b["open_time"])), "close": clean(b["close"]),
                      "sma200": clean(b["sma200"]), "above_sma200": bool(b["close"] > b["sma200"])}

    results, journal_rows, hist_rows = [], [], []
    hist_counts: Counter = Counter()
    partial_blocked: list[dict] = []
    for strategy in ALL:
        for interval in strategy.timeframes:
            block = {"strategy": strategy.id, "version": strategy.version, "params_hash": strategy.params_hash,
                     "name_he": strategy.name_he, "timeframe": interval, "min_candles": strategy.min_candles,
                     "signals": [], "watch": [], "insufficient": []}
            for sym, df in sorted(frames.get(interval, {}).items()):
                if sym not in universe or (strategy.id == "C-TRS" and sym == "BTCUSDT"):
                    continue
                ev = strategy.evaluate(df, {"rs": panel.get(sym)})
                last = len(ev) - 1
                st = ev["state"].iloc[last]
                s_row = snap.get(sym, {})
                if st == SIGNAL:
                    block["signals"].append(item(strategy, sym, interval, df, ev, last, s_row))
                elif st == WATCH:
                    block["watch"].append(item(strategy, sym, interval, df, ev, last, s_row))
                elif st == INSUFFICIENT:
                    block["insufficient"].append({"symbol": sym, "history_candles": len(df),
                                                  "needs": strategy.min_candles})
                # Journal: every confirmed signal; a WATCH only on the candle it begins.
                prev = ev["state"].iloc[last - 1] if last >= 1 else None
                if st == SIGNAL or (st == WATCH and prev != WATCH):
                    journal_rows.append((strategy, sym, interval, df, ev, last, s_row))
                # Historical occurrences (rules only; no outcome or performance).
                sig_idx = np.flatnonzero(ev["state"].to_numpy() == SIGNAL)
                for i in sig_idx:
                    hist_counts[(strategy.id, interval, pd.Timestamp(df.index[i]).year)] += 1
                    hist_rows.append(item(strategy, sym, interval, df, ev, i, {}))
                # Partial-candle check: candles where every signal rule held except "complete".
                others = [f"c_{k}" for k in strategy.signal_rules if k != "complete"]
                would = (ev[others] == 1).all(axis=1) & df["partial"].astype(bool)
                for i in np.flatnonzero(would.to_numpy()):
                    partial_blocked.append({"strategy": strategy.ref, "symbol": sym, "timeframe": interval,
                                            "candle_open_utc": iso(int(df["open_time"].iloc[i]))})
            for k in ("signals", "watch"):
                block[k].sort(key=lambda x: -(x["median_qvol_30d"] or 0))
            results.append(block)

    out = {"run": tag, "generated_utc": started.isoformat(timespec="seconds"), "source": settings["data"]["source"],
           "code_commit": commit, "universe_snapshot": snap_path.name, "universe_size": len(universe),
           "btc_regime": btc_regime, "stale": stale, "strategies": results,
           "disclaimers_he": [SURVIVORSHIP_HE, NOT_PROOF_HE,
                              "התראת מעקב (WATCH) אינה נקודת כניסה. רק איתות מאושר (SIGNAL) עומד בכל התנאים.",
                              "אין כאן ייעוץ השקעות. המערכת לא מבצעת מסחר ולא מחוברת לחשבון."]}
    scan_dir = ROOT / "data/scan"
    scan_dir.mkdir(parents=True, exist_ok=True)
    (scan_dir / f"{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (scan_dir / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    n_new = append_journal(journal_rows, started, commit, settings["data"]["source"])
    append_csv(ROOT / "journal/runs.csv", [{
        "run": tag, "started_utc": started.isoformat(timespec="seconds"), "code_commit": commit,
        "universe_snapshot": snap_path.name, "universe_size": len(universe), "stale_series": len(stale),
        "signals": sum(len(b["signals"]) for b in results), "watch": sum(len(b["watch"]) for b in results),
        "journal_rows_added": n_new}])

    rep = ROOT / "reports/phase2/runs" / tag
    rep.mkdir(parents=True, exist_ok=False)
    hist_cols = ["strategy", "strategy_version", "params_hash", "timeframe", "symbol", "candle_open_utc",
                 "ref_price", "stop", "target", "rr", "atr_pct", "partial_in_window", "history_candles", "reason_he"]
    with (rep / "historical_signals.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hist_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(hist_rows)
    (rep / "REPORT.md").write_text(render(out, hist_counts, hist_rows, partial_blocked, n_new))
    print((rep / "REPORT.md").read_text()[:6000])
    return 0


def append_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


def append_journal(rows, started: dt.datetime, commit: str, source: str) -> int:
    path = ROOT / "journal/signals.csv"
    existing = set()
    if path.exists():
        with path.open(newline="") as f:
            existing = {r["signal_id"] for r in csv.DictReader(f)}
    new = []
    for strategy, sym, interval, df, ev, i, s_row in rows:
        it = item(strategy, sym, interval, df, ev, i, s_row)
        key = f"{sym}|{strategy.ref}|{interval}|{it['state']}|{it['candle_open_utc']}"
        sid = hashlib.sha256(key.encode()).hexdigest()[:16]
        if sid in existing:
            continue  # same candle already journaled by an earlier run
        existing.add(sid)
        new.append({"signal_id": sid, "detected_at_utc": started.isoformat(timespec="seconds"),
                    "candle_open_utc": it["candle_open_utc"], "candle_close_utc": it["candle_close_utc"],
                    "exchange": source, "symbol": sym, "timeframe": interval, "strategy": strategy.id,
                    "strategy_version": strategy.version, "params_hash": strategy.params_hash, "type": it["state"],
                    "ref_price": it["ref_price"], "stop": it["stop"], "target": it["target"], "rr": it["rr"],
                    "atr_pct": it["atr_pct"], "realized_vol_30": it["realized_vol_30"],
                    "median_qvol_30d": it["median_qvol_30d"], "spread_pct": it["spread_pct"],
                    "liquidity_warnings": ";".join(it["liquidity_warnings"]),
                    "partial_in_window": it["partial_in_window"], "reason_he": it["reason_he"], "code_commit": commit})
    append_csv(path, new, JOURNAL_FIELDS)
    return len(new)


def f(x, d=4):
    return "—" if x is None else f"{x:,.{d}g}"


def render(out: dict, counts: Counter, hist: list[dict], partial_blocked: list[dict], n_new: int) -> str:
    L = [f"# דוח שלב 2 — אסטרטגיות ({out['run']})", "",
         f"- קוד: `{out['code_commit']}` · יקום: `{out['universe_snapshot']}` ({out['universe_size']} מטבעות) · "
         f"זמן: {out['generated_utc']}", "", "> " + SURVIVORSHIP_HE, ">", "> " + NOT_PROOF_HE, ""]
    b = out["btc_regime"]
    if b:
        L.append(f"- מצב BTC (מידע בלבד, לא מסנן): סגירה {fmt(b['close'])} "
                 f"{'מעל' if b['above_sma200'] else 'מתחת ל'} SMA200 {fmt(b['sma200'])}")
    if out["stale"]:
        L.append(f"- ⚠️ סדרות לא מעודכנות ({len(out['stale'])}) — לא נסרקו: " +
                 ", ".join(f"{s['symbol']} {s['timeframe']}" for s in out["stale"][:20]))
    L += ["", "## כללי האסטרטגיות (נוצר מהקוד)", ""]
    for s in ALL:
        L += [f"### {s.name_he} — `{s.ref}` · hash `{s.params_hash}` · טווחים: {', '.join(s.timeframes)} · "
              f"נדרשים {s.min_candles} נרות", "",
              "| # | כלל | מעקב 🟡 | איתות 🟢 |", "|---|---|---|---|"]
        for i, r in enumerate(s.rules, 1):
            L.append(f"| {i} | {r.label_he} | {'✓' if r.key in s.watch_rules else ''} | {'✓' if r.key in s.signal_rules else ''} |")
        L += ["", f"פרמטרים: `{json.dumps(s.params, ensure_ascii=False)}`", ""]

    L += ["## הסריקה הנוכחית (הנר הסגור האחרון)", ""]
    for blk in out["strategies"]:
        L += [f"### {blk['name_he']} · {blk['timeframe']}", "",
              f"- 🟢 איתותים מאושרים: {len(blk['signals'])} · 🟡 התראות מעקב: {len(blk['watch'])} · "
              f"היסטוריה לא מספיקה: {len(blk['insufficient'])}"
              + (f" ({', '.join(x['symbol'][:-4] + ' ' + str(x['history_candles']) + '/' + str(x['needs']) for x in blk['insufficient'])})"
                 if blk["insufficient"] else ""), ""]
        for kind, icon in (("signals", "🟢 איתות"), ("watch", "🟡 מעקב")):
            for x in blk[kind]:
                warn = f" · ⚠️ נזילות: {', '.join(x['liquidity_warnings'])}" if x["liquidity_warnings"] else ""
                part = " · ⚠️ נר חלקי ב-20 הנרות האחרונים" if x["partial_in_window"] else ""
                if kind == "signals":
                    rr = f(x["rr"], 3) if x["rr"] is not None else x["rr_note"]
                    levels = (f"ביטול {fmt(x['stop'])} ({f(x['risk_pct_to_stop'], 3)}%) · יעד {fmt(x['target'])} · R:R {rr}")
                else:
                    levels = x["watch_invalidation_he"] + " · אין כניסה/סטופ/יעד עד אישור"
                L += [f"- **{icon} {x['base']}** · נר {x['candle_open_utc']} · מחיר {fmt(x['ref_price'])} · {levels} · "
                      f"ATR {f(x['atr_pct'], 3)}% · תנודתיות 30 {f(x['realized_vol_30'], 3)}{warn}{part}",
                      f"  - {x['reason_he']}"]
        L.append("")
    L += [f"- שורות חדשות ביומן: {n_new}", ""]

    L += ["## היכן הכללים התקיימו בהיסטוריה (ספירה בלבד, ללא ביצועים)", "",
          "| אסטרטגיה | טווח | " + " | ".join(str(y) for y in range(2017, 2027)) + " | סה\"כ |",
          "|---|---|" + "---|" * 11]
    for s in ALL:
        for itv in s.timeframes:
            row = [counts.get((s.id, itv, y), 0) for y in range(2017, 2027)]
            L.append(f"| {s.id} | {itv} | " + " | ".join(str(v) for v in row) + f" | {sum(row)} |")
    L += ["", "## דוגמאות אמיתיות (3 האיתותים ההיסטוריים האחרונים לכל אסטרטגיה וטווח)", ""]
    by = defaultdict(list)
    for h in hist:
        by[(h["strategy"], h["timeframe"])].append(h)
    for (sid, itv), items in sorted(by.items()):
        L += [f"### {sid} · {itv}", ""]
        for x in sorted(items, key=lambda x: x["candle_open_utc"])[-3:]:
            rr = f(x["rr"], 3) if x["rr"] is not None else x["rr_note"]
            L += [f"- **{x['base']}** · נר {x['candle_open_utc']} · מחיר {fmt(x['ref_price'])} · ביטול {fmt(x['stop'])} · "
                  f"יעד {fmt(x['target'])} · R:R {rr}" + (" · ⚠️ נר חלקי בחלון" if x["partial_in_window"] else ""),
                  f"  - {x['reason_he']}"]
        L.append("")
    near = sum(1 for h in hist if h["partial_in_window"])
    L += ["## נרות חלקיים (עצירות מסחר)", "",
          f"- איתותים שהיו נוצרים **על נר חלקי** אילולא הכלל 'נר שלם' (נחסמו): {len(partial_blocked)}"]
    L += [f"  - {p['strategy']} {p['symbol']} {p['timeframe']} {p['candle_open_utc']}" for p in partial_blocked[:20]]
    L += [f"- איתותים היסטוריים שיש נר חלקי ב-20 הנרות שלפניהם (מסומנים ⚠️, השפעתם תיבדק בנפרד בשלב 5): "
          f"{near} מתוך {len(hist)}", ""]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
