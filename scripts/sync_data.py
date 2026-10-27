"""Phase 1 data run: dated listing + universe + liquidity snapshots, full paged OHLCV history sync with
integrity checks, and BTC indicator values for manual cross-checking.

Outputs (all new files per run, except the current month's candle files which are appended to):
  data/universe/exchange/<date>-<run>.csv     every spot symbol and its status (survivorship record)
  data/universe/snapshots/<date>-<run>.csv    every USDT candidate with status, flags and measured values
  data/liquidity/<date>-<run>.csv             order-book metrics for pairs above the liquidity floor
  data/ohlcv/binance/<interval>/<SYMBOL>/<YYYY-MM>.csv
  data/ohlcv/binance/manifest.json            current coverage, gaps and revisions per series
  data/ohlcv/binance/events.csv               append-only log of revisions and new gaps
  reports/phase1/runs/<run>/REPORT.md, summary.json

Read-only public market data. No keys, no account or order endpoints.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from radar import config, history, indicators, liquidity, universe  # noqa: E402
from radar.sources import binance  # noqa: E402
from radar.sources.base import now_ms  # noqa: E402
from radar.store import CandleStore, iso  # noqa: E402

WORKERS = 4


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)  # snapshots are never overwritten
    cols: list[str] = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def liquidity_history(symbols: set[str]) -> dict[str, list[dict]]:
    """Earlier order-book measurements per symbol from data/liquidity/*.csv, oldest first."""
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    for path in sorted((ROOT / "data/liquidity").glob("*.csv")):
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                if row["symbol"] in out and row.get("measured_utc"):
                    out[row["symbol"]].append(liquidity.parse_measurement(row))
    for v in out.values():
        v.sort(key=lambda m: m["measured_utc"])
    return out


def build_universe(settings: dict, exclusions: dict, review: dict, base_url: str,
                   started_iso: str) -> tuple[list[universe.Candidate], list[dict], dict]:
    u, chk, liq = settings["universe"], settings["asset_checks"], settings["liquidity"]
    all_symbols = binance.exchange_symbols(base_url)
    usdt = [m for m in all_symbols if m["quoteAsset"] == "USDT" and m["status"] == "TRADING"
            and m.get("isSpotTradingAllowed", True)]
    all_bases = {m["baseAsset"] for m in usdt}
    common = universe.common_permission_groups(usdt, chk["common_permission_share"])
    vol24 = binance.tickers_24h(base_url)

    cands: list[universe.Candidate] = []
    meta_by_symbol = {m["symbol"]: m for m in usdt}
    for m in usdt:
        c = universe.Candidate(m["symbol"], m["baseAsset"])
        if m["baseAsset"] in exclusions:
            c.status, c.exclusion = "excluded", exclusions[m["baseAsset"]]
        elif universe.leveraged_token(m["baseAsset"], all_bases):
            c.status, c.exclusion = "excluded", {"category": "leveraged_token", "reason": "leveraged token"}
        elif vol24.get(m["symbol"], 0) < u["prefilter_24h_quote_volume_usd"]:
            c.status = "below_prefilter"
        cands.append(c)

    history_by_symbol = liquidity_history({c.symbol for c in cands if not c.status})

    def measure(c: universe.Candidate) -> None:
        candles = binance.klines(c.symbol, "1d", limit=u["volume_lookback_days"] + 1, base_url=base_url)
        candles = candles[-u["volume_lookback_days"]:]
        c.days = len(candles)
        c.median_qvol_30d = statistics.median([x.quote_volume for x in candles]) if candles else 0.0
        c.flags, c.checks = universe.asset_flags(candles, meta_by_symbol[c.symbol], common, chk)
        if c.status:  # explicitly excluded pair measured for evidence only (would the automated checks catch it?)
            return
        if c.median_qvol_30d >= u["min_median_quote_volume_usd"]:
            bids, asks = binance.order_book(c.symbol, liq["depth_limit"], base_url)
            m = liquidity.book_metrics(bids, asks, liq["depth_bands_pct"], liq["impact_sizes_usd"])
            c.checks["liquidity"] = m
            c.liquidity_warnings = liquidity.liquidity_warnings(m, liq["max_spread_pct"], liq["min_depth_1pct_usd"])
            series = history_by_symbol.get(c.symbol, []) + [{"measured_utc": started_iso} | m]
            c.checks["liquidity_persistence"] = p = liquidity.persistence(series, liq)
            c.liquidity_suspended = p["suspended"]
            c.tradable, c.checks["tradability"] = universe.tradability(candles, bool(bids) and bool(asks),
                                                                         u["tradability_days"])

    evidence_only = [c for c in cands if c.status == "excluded" and (c.exclusion or {}).get("category") == "tokenized_equity"]
    todo = [c for c in cands if not c.status] + evidence_only
    errors = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for c, fut in [(c, pool.submit(measure, c)) for c in todo]:
            try:
                fut.result()
            except Exception as exc:
                errors[c.symbol] = str(exc)[:200]
                if not c.status:
                    c.status = "fetch_error"
    universe.finalize([c for c in cands if c.status != "fetch_error"], u["min_median_quote_volume_usd"],
                      u["max_size"], u["volume_lookback_days"], review)
    return cands, all_symbols, {"common_permission_groups": sorted(common), "fetch_errors": errors}


def sync_histories(symbols: list[str], settings: dict, store: CandleStore, base_url: str) -> list[history.SyncResult]:
    overlap = settings["data"]["sync_overlap_candles"]
    scan = now_ms()

    def fetch(symbol: str, interval: str, start: int):
        return binance.klines(symbol, interval, limit=binance.MAX_KLINES, start_ms=start, base_url=base_url)

    def one(job):
        sym, itv = job
        try:
            return history.sync(store, fetch, sym, itv, binance.INTERVAL_MS[itv], scan, overlap)
        except Exception as exc:
            r = history.SyncResult(sym, itv)
            r.error = str(exc)[:300]
            return r

    jobs = [(s, i) for s in symbols for i in settings["data"]["intervals"]]
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(one, jobs))


def btc_indicator_check(store: CandleStore) -> dict:
    out = {}
    for itv in ("1d", "4h"):
        df = indicators.compute_all(indicators.to_frame(store.load("BTCUSDT", itv)), itv)
        if df.empty:
            continue
        last = df.iloc[-1]
        out[itv] = {"candle_open_utc": iso(int(last["open_time"])), "close": last["close"]} | {
            k: (None if pd.isna(last[k]) else round(float(last[k]), 4))
            for k in ("sma20", "sma50", "sma200", "atr14", "atr_pct", "bb_upper", "bb_mid", "bb_lower",
                      "bb_width", "bb_width_pct", "realized_vol_30")}
    return out


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID")
    tag = f"{started:%Y-%m-%d}-run-{run_id}" if run_id else f"{started:%Y-%m-%d}-local-{started:%H%M%S}"
    settings, exclusions, review = config.load_settings(), config.load_exclusions(), config.load_review()
    base_url = settings["data"]["base_url"]
    store = CandleStore(ROOT / settings["data"]["store_dir"], settings["data"]["source"])

    t0 = time.monotonic()
    cands, all_symbols, uni_meta = build_universe(settings, exclusions, review, base_url,
                                                  started.isoformat(timespec="seconds"))
    t_universe = time.monotonic() - t0

    write_csv(ROOT / "data/universe/exchange" / f"{tag}.csv",
              [{"symbol": m["symbol"], "base": m["baseAsset"], "quote": m["quoteAsset"], "status": m["status"]}
               for m in all_symbols])
    snap_rows = []
    for c in sorted(cands, key=lambda c: -(c.median_qvol_30d or 0)):
        liq = c.checks.get("liquidity", {})
        pers = c.checks.get("liquidity_persistence", {})
        snap_rows.append({
            "symbol": c.symbol, "base": c.base, "status": c.status, "flags": ";".join(c.flags),
            "review_reason": c.review_reason, "notes": " | ".join(c.notes), "tradable": c.tradable,
            "liquidity_warnings": ";".join(c.liquidity_warnings),
            "liquidity_hits": pers.get("hits"), "liquidity_measurements": pers.get("measurements"),
            "liquidity_span_hours": pers.get("span_hours"),
            "median_quote_volume_30d": None if c.median_qvol_30d is None else round(c.median_qvol_30d),
            "days": c.days, "exclusion_category": (c.exclusion or {}).get("category"),
            "range_30d_pct": c.checks.get("range_30d_pct"),
            "weekend_volume_ratio": c.checks.get("weekend_volume_ratio"),
            "missing_common_permission_groups": ";".join(c.checks.get("missing_common_permission_groups", [])),
            "spread_pct": liq.get("spread_pct"), "depth_bid_1pct_usd": liq.get("depth_bid_1.0pct_usd"),
            "depth_ask_1pct_usd": liq.get("depth_ask_1.0pct_usd"),
            "source": settings["data"]["source"], "snapshot_utc": started.isoformat(timespec="seconds"),
            "settings_fingerprint": config.fingerprint(settings),
        })
    write_csv(ROOT / "data/universe/snapshots" / f"{tag}.csv", snap_rows)
    write_csv(ROOT / "data/liquidity" / f"{tag}.csv",
              [{"symbol": c.symbol, "status": c.status, "measured_utc": started.isoformat(timespec="seconds")}
               | c.checks["liquidity"] for c in cands if "liquidity" in c.checks])

    included = [c.symbol for c in cands if c.status == "included"]
    if "BTCUSDT" not in included:
        included.insert(0, "BTCUSDT")  # needed for relative strength regardless of its own status
    t0 = time.monotonic()
    results = sync_histories(included, settings, store, base_url)
    t_history = time.monotonic() - t0

    manifest_path = store.root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    events = []
    for r in results:
        rep = r.report
        manifest.setdefault(r.symbol, {})[r.interval] = {
            "first": rep.first if rep else None, "last": rep.last if rep else None,
            "count": rep.count if rep else 0, "gaps": rep.gaps if rep else [],
            "partial_candles": rep.examples.get("partial", []) if rep else [], "partial_count": rep.partial if rep else 0,
            "last_sync_utc": started.isoformat(timespec="seconds"), "last_error": r.error}
        for rev in r.revisions:
            events.append({"utc": started.isoformat(timespec="seconds"), "run": tag, "symbol": r.symbol,
                           "interval": r.interval, "event": "revision", "detail": json.dumps(rev)})
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    if events:
        ev_path = store.root / "events.csv"
        new = not ev_path.exists()
        with ev_path.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0]))
            if new:
                w.writeheader()
            w.writerows(events)

    summary = {"run": tag, "started_utc": started.isoformat(timespec="seconds"), "source": settings["data"]["source"],
               "settings_fingerprint": config.fingerprint(settings), "seconds_universe": round(t_universe),
               "seconds_history": round(t_history), "universe_meta": uni_meta,
               "status_counts": {s: sum(1 for c in cands if c.status == s) for s in sorted({c.status for c in cands})},
               "btc_indicators": btc_indicator_check(store),
               "sync": [{"symbol": r.symbol, "interval": r.interval, "pages": r.pages, "appended": r.appended,
                         "revisions": len(r.revisions), "error": r.error,
                         "count": r.report.count if r.report else 0, "first": r.report.first if r.report else None,
                         "last": r.report.last if r.report else None,
                         "gaps": len(r.report.gaps) if r.report else 0,
                         "missing_candles": sum(g["missing_candles"] for g in r.report.gaps) if r.report else 0,
                         "partial": r.report.partial if r.report else 0,
                         "defects": ({k: getattr(r.report, k) for k in ("duplicates", "unordered", "unclosed", "misaligned", "bad_ohlc")}
                                     if r.report else {}),
                         "defect_examples": r.report.examples if r.report else {}}
                        for r in results]}
    rep_dir = ROOT / "reports/phase1/runs" / tag
    rep_dir.mkdir(parents=True, exist_ok=False)
    (rep_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    (rep_dir / "REPORT.md").write_text(render(summary, cands, settings))
    print((rep_dir / "REPORT.md").read_text())
    return 1 if any(r.error for r in results) or uni_meta["fetch_errors"] else 0


def fmt_usd(x) -> str:
    return "—" if x is None else f"${x/1e6:,.1f}M"


def render(s: dict, cands: list[universe.Candidate], settings: dict) -> str:
    min_hist = settings["universe"]["min_history_candles"]
    L = [f"# דוח נתונים ({s['run']})", "",
         f"- מקור: `{s['source']}` · הגדרות: `{s['settings_fingerprint']}` · זמן: {s['started_utc']}",
         f"- משך: יקום {s['seconds_universe']} ש׳, היסטוריה {s['seconds_history']} ש׳", "",
         "## יקום", "", "| סטטוס | כמות |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in s["status_counts"].items()]
    L += ["", "### נכללים בסריקה", "",
          "| # | מטבע | חציון מחזור 30 יום | ספרד % | עומק ±1% קנייה / מכירה | השפעת מחיר בקנייה של $10K | השפעת מחיר בקנייה של $50K | אזהרות נזילות (פגיעות/מדידות) | הערות |",
          "|---|---|---|---|---|---|---|---|---|"]
    inc = sorted([c for c in cands if c.status == "included"], key=lambda c: -c.median_qvol_30d)
    for i, c in enumerate(inc, 1):
        m = c.checks.get("liquidity", {})
        imp = lambda k: "—" if m.get(k) is None else f"{m[k]:.3f}%"
        dep = lambda side: ("≥" if m.get(f"depth_{side}_1.0pct_lower_bound") else "") + fmt_usd(m.get(f"depth_{side}_1.0pct_usd"))
        L.append(f"| {i} | {c.base} | {fmt_usd(c.median_qvol_30d)} | {m.get('spread_pct', 0):.4f} | "
                 f"{dep('bid')} / {dep('ask')} | "
                 f"{imp('impact_buy_10000_pct')} | {imp('impact_buy_50000_pct')} | "
                 f"{', '.join(c.liquidity_warnings) or '—'} ({c.checks.get('liquidity_persistence', {}).get('hits', 0)}/"
                 f"{c.checks.get('liquidity_persistence', {}).get('measurements', 0)}) | {' · '.join(c.notes) or ''} |")
    rev = [c for c in cands if c.status == "review"]
    L += ["", "### תור בדיקת חריגים (לא נסרקים)", ""]
    if rev:
        L += ["| מטבע | חציון מחזור | סיבה | דגלי סוג נכס | קבוצות הרשאה חסרות | טווח מחיר 30 יום | יחס נפח סופ\"ש | אזהרות נזילות (פגיעות/מדידות, טווח שעות) |",
              "|---|---|---|---|---|---|---|---|"]
        for c in sorted(rev, key=lambda c: -c.median_qvol_30d):
            r, w = c.checks.get("range_30d_pct"), c.checks.get("weekend_volume_ratio")
            p = c.checks.get("liquidity_persistence", {})
            L.append(f"| {c.base} | {fmt_usd(c.median_qvol_30d)} | {c.review_reason} | {', '.join(c.flags) or '—'} | "
                     f"{', '.join(c.checks.get('missing_common_permission_groups', [])) or '—'} | "
                     f"{'—' if r is None else f'{r:.1f}%'} | {'—' if w is None else f'{w:.2f}'} | "
                     f"{', '.join(c.liquidity_warnings) or '—'} ({p.get('hits', 0)}/{p.get('measurements', 0)}, {p.get('span_hours', 0)}h) |")
    else:
        L.append("- ריק")
    sus = [c for c in cands if c.status == "liquidity_suspended"]
    L += ["", "### מושעים בגלל נזילות מתמשכת", ""]
    L += [f"- {c.base}: {c.checks['liquidity_persistence']}" for c in sus] or ["- אין"]
    ev = [c for c in cands if c.status == "excluded" and "range_30d_pct" in c.checks]
    if ev:
        caught = [c for c in ev if c.flags]
        L += ["", "### ראיה: האם הבדיקות האוטומטיות היו תופסות את המניות בטוקן בלי הרשימה המפורשת?", "",
              f"- נתפסו: {len(caught)} מתוך {len(ev)}", "",
              "| נכס | דגלים | קבוצות חסרות | יחס נפח סופ\"ש | טווח 30 יום |", "|---|---|---|---|---|"]
        for c in sorted(ev, key=lambda c: c.base):
            w, r = c.checks.get("weekend_volume_ratio"), c.checks.get("range_30d_pct")
            L.append(f"| {c.base} | {', '.join(c.flags) or '—'} | {', '.join(c.checks.get('missing_common_permission_groups', [])) or '—'} | "
                     f"{'—' if w is None else f'{w:.2f}'} | {'—' if r is None else f'{r:.1f}%'} |")
    exc = [c for c in cands if c.status == "excluded"]
    L += ["", f"### הוחרגו במפורש ({len(exc)})", "",
          ", ".join(f"{c.base} ({c.exclusion['category']})" for c in sorted(exc, key=lambda c: c.base))]
    if s["universe_meta"]["fetch_errors"]:
        L += ["", "### שגיאות טעינה", ""] + [f"- {k}: {v}" for k, v in s["universe_meta"]["fetch_errors"].items()]

    L += ["", "## היסטוריית נרות", "",
          "| מטבע | טווח | נרות | ראשון | אחרון | נוספו בהרצה | דפים | חורים (נרות חסרים) | נרות חלקיים (עצירת מסחר) | תיקוני בורסה | מצב |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(s["sync"], key=lambda r: (r["symbol"], r["interval"])):
        state = r["error"] or ("היסטוריה לא מספיקה" if r["count"] < min_hist else "תקין")
        L.append(f"| {r['symbol'][:-4]} | {r['interval']} | {r['count']} | {r['first']} | {r['last']} | {r['appended']} | "
                 f"{r['pages']} | {r['gaps']} ({r['missing_candles']}) | {r.get('partial', 0)} | {r['revisions']} | {state} |")
    bad = [r for r in s["sync"] if any(r.get("defects", {}).values())]
    if bad:
        L += ["", "## פגמים שנמצאו בנתונים (הסדרה לא נשמרה)", ""]
        for r in sorted(bad, key=lambda r: (r["symbol"], r["interval"])):
            counts = ", ".join(f"{k}={v}" for k, v in r["defects"].items() if v)
            L.append(f"- **{r['symbol']} {r['interval']}**: {counts}")
            for kind, ex in ((k, v) for k, v in r["defect_examples"].items() if k != "partial"):
                for e in ex[:2]:
                    L.append(f"  - {kind}: `{json.dumps(e)}`")
    L += ["", "## ערכי אינדיקטורים ל-BTCUSDT (להשוואה ידנית מול TradingView, Binance, נרות UTC)", ""]
    for itv, v in s["btc_indicators"].items():
        L += [f"### {itv} — נר שנפתח ב-{v['candle_open_utc']}", "", "| מדד | ערך |", "|---|---|"]
        L += [f"| {k} | {val} |" for k, val in v.items() if k != "candle_open_utc"]
        L.append("")
    L += ["> כל הנתונים נמשכו בזמן ההרצה מה-API הציבורי של Binance. חורים בנתונים מדווחים כפי שהם ואינם מושלמים.",
          "> נתוני ספרד ועומק הם תמונת מצב רגעית של ספר הפקודות; אין להם היסטוריה ציבורית."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
