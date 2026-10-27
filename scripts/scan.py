"""Run the three strategies on the latest closed candles of the current universe.

Writes:
  data/scan/<tag>.json           full scan result of this run (new file per run)
  data/scan/latest.json          the same, as the current state for the dashboard (overwritten each run)
  journal/signals-v2.csv         append-only: every live SIGNAL and WATCH episode, with its origin (live_new /
                                 live_already_active). journal/signals.csv is the frozen v1 journal.
  journal/runs.csv               append-only: one row per scan
  reports/scan/runs/<tag>/       REPORT.md (Hebrew) + historical_signals.csv — where the rules fired in the
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

from radar import config, engine, indicators  # noqa: E402
from radar.store import CandleStore, iso  # noqa: E402
from radar.strategies import ALL, INSUFFICIENT, SIGNAL, WATCH  # noqa: E402
from radar.strategies.base import fmt  # noqa: E402
from radar.present import clean, item, num  # noqa: E402,F401

SURVIVORSHIP_HE = ("**הטיית שורדים:** הנתונים ההיסטוריים כוללים רק מטבעות שנסחרים היום ב-Binance וברשימה הנוכחית. "
                   "מטבעות שנמחקו או שירדו מהרשימה בעבר חסרים, ולכן כל ספירה או תוצאה היסטורית עלולה להיות "
                   "אופטימית מהמציאות. תיעוד מתוארך של הרשימה קיים רק מ-27.10.2026.")
NOT_PROOF_HE = ("**אין כאן הוכחה לרווחיות.** הדוח מציג היכן הכללים התקיימו בעבר, ללא חישוב ביצועים. "
                "גם בדיקה היסטורית עתידית (שלב 5) לא תהיה הוכחה לתוצאות עתידיות.")
JOURNAL_V2_FIELDS = ["signal_id", "origin", "detected_at_utc", "detection_delay_min", "episode_started_utc",
                     "candle_open_utc", "candle_close_utc", "exchange", "symbol", "timeframe", "strategy",
                     "strategy_version", "params_hash", "type", "ref_price", "stop", "target", "rr", "high_risk",
                     "watch_invalidation", "breakout_level", "atr_pct", "realized_vol_30", "median_qvol_30d",
                     "spread_pct", "liquidity_warnings", "partial_in_window", "reason_he", "code_commit"]
JOURNAL_V1_FIELDS = ["signal_id", "detected_at_utc", "candle_open_utc", "candle_close_utc", "exchange", "symbol",
                  "timeframe", "strategy", "strategy_version", "params_hash", "type", "ref_price", "stop", "target",
                  "rr", "atr_pct", "realized_vol_30", "median_qvol_30d", "spread_pct", "liquidity_warnings",
                  "partial_in_window", "reason_he", "code_commit"]


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
    high_risk_pct = settings["risk"]["high_risk_stop_pct"]

    raw: dict[str, dict[str, pd.DataFrame]] = {}
    stale: list[dict] = []
    for interval in settings["data"]["intervals"]:
        span = indicators.INTERVAL_MS[interval]
        expected_last_open = (now_ms // span) * span - span
        loaded = engine.load_candles(store, sorted(set(universe) | {"BTCUSDT"}), interval, now_ms)
        raw[interval] = {}
        for sym in sorted(set(universe) | {"BTCUSDT"}):
            df = loaded.get(sym)
            if df is None or int(df["open_time"].iloc[-1]) != expected_last_open:
                stale.append({"symbol": sym, "timeframe": interval,
                              "last_stored": iso(int(df["open_time"].iloc[-1])) if df is not None else None,
                              "expected": iso(expected_last_open)})
                continue
            raw[interval][sym] = df
    res = engine.run(raw, settings["universe"], set(universe), ALL, as_of_ms=now_ms)

    btc_daily = res.frames.get("1d", {}).get("BTCUSDT")
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
                     "signals": [], "watch": [], "insufficient": [], "not_member_now": []}
            for sym, ev in sorted(res.evaluated[(strategy.id, interval)].items()):
                if sym not in universe:
                    continue
                df = res.frames[interval][sym]
                last = len(ev) - 1
                st = ev["state"].iloc[last]
                s_row = snap.get(sym, {})
                if st in (SIGNAL, WATCH):
                    it = item(strategy, sym, interval, df, ev, last, s_row, high_risk_pct)
                    block["signals" if st == SIGNAL else "watch"].append(it)
                    journal_rows.append((it, engine.episode_start(ev["state"], last), df))
                elif st == INSUFFICIENT:
                    block["insufficient"].append({"symbol": sym, "history_candles": len(df),
                                                  "needs": strategy.min_candles})
                elif st == engine.NOT_MEMBER:
                    block["not_member_now"].append(sym)
                # Historical occurrences (rules only; no outcome or performance). Member candles only.
                for i in np.flatnonzero(ev["state"].to_numpy() == SIGNAL):
                    hist_counts[(strategy.id, interval, pd.Timestamp(df.index[i]).year)] += 1
                    hist_rows.append(item(strategy, sym, interval, df, ev, i, {}, high_risk_pct))
                # Partial-candle check: candles where every signal rule held except "complete".
                others = [f"c_{k}" for k in strategy.signal_rules if k != "complete"]
                would = (ev[others] == 1).all(axis=1) & df["partial"].astype(bool) & ev["member"].astype(bool)
                for i in np.flatnonzero(would.to_numpy()):
                    partial_blocked.append({"strategy": strategy.ref, "symbol": sym, "timeframe": interval,
                                            "candle_open_utc": iso(int(df["open_time"].iloc[i]))})
            for k in ("signals", "watch"):
                block[k].sort(key=lambda x: -(x["median_qvol_30d"] or 0))
            results.append(block)

    out = {"run": tag, "generated_utc": started.isoformat(timespec="seconds"), "source": settings["data"]["source"],
           "code_commit": commit, "data_fingerprint": res.data_fingerprint, "universe_snapshot": snap_path.name,
           "universe_size": len(universe), "high_risk_stop_pct": high_risk_pct,
           "btc_regime": btc_regime, "stale": stale, "strategies": results,
           "disclaimers_he": [SURVIVORSHIP_HE, NOT_PROOF_HE,
                              "התראת מעקב (WATCH) אינה נקודת כניסה. רק איתות מאושר (SIGNAL) עומד בכל התנאים.",
                              "שום איתות אינו המלצת כניסה. איתות שהסטופ שלו רחוק מ-"
                              f"{high_risk_pct:g}% מסומן 'סיכון גבוה'.",
                              "אין כאן ייעוץ השקעות. המערכת לא מבצעת מסחר ולא מחוברת לחשבון."]}
    scan_dir = ROOT / "data/scan"
    scan_dir.mkdir(parents=True, exist_ok=True)
    (scan_dir / f"{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (scan_dir / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    added = append_journal(journal_rows, started, commit, settings["data"]["source"])
    append_csv(ROOT / "journal/runs.csv", [{
        "run": tag, "started_utc": started.isoformat(timespec="seconds"), "code_commit": commit,
        "universe_snapshot": snap_path.name, "universe_size": len(universe), "stale_series": len(stale),
        "signals": sum(len(b["signals"]) for b in results), "watch": sum(len(b["watch"]) for b in results),
        "journal_rows_added": sum(added.values())}])

    rep = ROOT / "reports/scan/runs" / tag
    rep.mkdir(parents=True, exist_ok=False)
    hist_cols = ["strategy", "strategy_version", "params_hash", "timeframe", "symbol", "candle_open_utc",
                 "ref_price", "stop", "target", "rr", "risk_pct_to_stop", "high_risk", "atr_pct",
                 "partial_in_window", "history_candles", "reason_he"]
    with (rep / "historical_signals.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=hist_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(hist_rows)
    (rep / "REPORT.md").write_text(render(out, hist_counts, hist_rows, partial_blocked, added))
    print((rep / "REPORT.md").read_text()[:3000])
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


def journal_key(it: dict, episode_start_utc: str) -> str:
    """Stable id of one alert episode. For a SIGNAL the episode is its own candle; for a WATCH it is the run of
    consecutive WATCH candles, identified by its first candle. Same formula as journal v1 (whose WATCH rows were
    all recorded on their first candle), so a v1 row and a v2 row of the same episode share an id."""
    key = f"{it['symbol']}|{it['strategy']}@{it['strategy_version']}|{it['timeframe']}|{it['state']}|{episode_start_utc}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def journal_ids() -> set[str]:
    ids = set()
    for name in ("signals.csv", "signals-v2.csv"):
        path = ROOT / "journal" / name
        if path.exists():
            with path.open(newline="") as f:
                ids |= {r["signal_id"] for r in csv.DictReader(f)}
    return ids


def append_journal(rows, started: dt.datetime, commit: str, source: str) -> dict[str, int]:
    """Append live alerts to journal/signals-v2.csv (journal/signals.csv is the frozen v1 file).

    origin = live_new             the alert's first candle is the candle this scan evaluated
             live_already_active  the alert was already active when first observed by a live scan (e.g. it began
                                  before the journal existed). episode_started_utc is computed from the stored
                                  candles and is NOT the time it was detected; detected_at_utc is the real
                                  observation time. Nothing is back-dated.
    Historical reconstructions (Time Machine) are never written here; they live under data/replays/.
    """
    existing = journal_ids()
    new, counts = [], Counter()
    for it, ep_start, df in rows:
        ep_utc = iso(int(df["open_time"].iloc[ep_start]))
        sid = journal_key(it, ep_utc)
        if sid in existing:
            continue  # this episode is already in the journal (v1 or v2)
        existing.add(sid)
        origin = "live_new" if ep_utc == it["candle_open_utc"] else "live_already_active"
        counts[origin] += 1
        close_dt = dt.datetime.fromisoformat(it["candle_close_utc"].replace("Z", "+00:00"))
        new.append({"signal_id": sid, "origin": origin, "detected_at_utc": started.isoformat(timespec="seconds"),
                    "detection_delay_min": round((started - close_dt).total_seconds() / 60, 1),
                    "episode_started_utc": ep_utc, "candle_open_utc": it["candle_open_utc"],
                    "candle_close_utc": it["candle_close_utc"], "exchange": source, "symbol": it["symbol"],
                    "timeframe": it["timeframe"], "strategy": it["strategy"], "strategy_version": it["strategy_version"],
                    "params_hash": it["params_hash"], "type": it["state"], "ref_price": it["ref_price"],
                    "stop": it["stop"], "target": it["target"], "rr": it["rr"], "high_risk": it["high_risk"],
                    "watch_invalidation": it["watch_invalidation"], "breakout_level": it["breakout_level"],
                    "atr_pct": it["atr_pct"], "realized_vol_30": it["realized_vol_30"],
                    "median_qvol_30d": it["median_qvol_30d"], "spread_pct": it["spread_pct"],
                    "liquidity_warnings": ";".join(it["liquidity_warnings"]),
                    "partial_in_window": it["partial_in_window"], "reason_he": it["reason_he"], "code_commit": commit})
    append_csv(ROOT / "journal/signals-v2.csv", new, JOURNAL_V2_FIELDS)
    return dict(counts)


def f(x, d=4):
    return "—" if x is None else f"{x:,.{d}g}"


def render(out: dict, counts: Counter, hist: list[dict], partial_blocked: list[dict], added: dict) -> str:
    L = [f"# דוח סריקה ({out['run']})", "",
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
                    if x["high_risk"]:
                        warn += f" · ⚠️ סיכון גבוה: הסטופ במרחק {f(x['risk_pct_to_stop'], 3)}% — לא המלצת כניסה"
                    rr = f(x["rr"], 3) if x["rr"] is not None else x["rr_note"]
                    levels = (f"ביטול {fmt(x['stop'])} ({f(x['risk_pct_to_stop'], 3)}%) · יעד {fmt(x['target'])} · R:R {rr}")
                else:
                    levels = x["watch_invalidation_he"] + " · אין כניסה/סטופ/יעד עד אישור"
                L += [f"- **{icon} {x['base']}** · נר {x['candle_open_utc']} · מחיר {fmt(x['ref_price'])} · {levels} · "
                      f"ATR {f(x['atr_pct'], 3)}% · תנודתיות 30 {f(x['realized_vol_30'], 3)}{warn}{part}",
                      f"  - {x['reason_he']}"]
        L.append("")
    L += [f"- רשומות חדשות ביומן: {sum(added.values())} (חדשות בזמן אמת: {added.get('live_new', 0)} · "
          f"היו פעילות כבר לפני שנצפו: {added.get('live_already_active', 0)})", ""]

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
