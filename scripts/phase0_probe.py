"""Phase 0: verify access to public market-data sources from the run environment, cross-check them,
and build the first liquidity-ranked universe from the chosen source.

Writes one new directory per run (never overwrites a previous run's output):
  reports/phase0/runs/<run>/probe.json       machine-readable results
  reports/phase0/runs/<run>/REPORT.md        Hebrew summary
  data/universe/universe-<date>-<run>.csv

Read-only: public market-data endpoints only. No keys, no account or order endpoints.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import universe  # noqa: E402
from radar.sources import binance, okx  # noqa: E402
from radar.sources.base import SourceError, get_json, now_ms  # noqa: E402

DAY_MS = 86_400_000
H4_MS = 14_400_000
ROUNDS = 3
PREFILTER_24H_USD = 250_000
FLOOR_SENSITIVITY = (5_000_000, 3_000_000, 2_000_000, 1_000_000)

REACHABILITY = {
    "binance_vision": f"{binance.BASE_URL}/api/v3/ping",
    "binance_api": "https://api.binance.com/api/v3/ping",
    "okx": f"{okx.BASE_URL}/api/v5/public/time",
    "coinbase": "https://api.exchange.coinbase.com/time",
    "kraken": "https://api.kraken.com/0/public/Time",
}


def utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def check_reachability() -> dict:
    out = {}
    for name, url in REACHABILITY.items():
        ok, statuses, latencies, errors = 0, [], [], []
        for _ in range(ROUNDS):
            try:
                _, status, latency = get_json(url, retries=1)
                ok += 1
                statuses.append(status)
                latencies.append(round(latency))
            except Exception as exc:  # record every failure verbatim
                errors.append(str(exc)[:300])
            time.sleep(0.5)
        out[name] = {"url": url, "ok_rounds": ok, "rounds": ROUNDS, "statuses": statuses,
                     "latency_ms": latencies, "errors": errors}
    return out


def validate_candles(candles, span_ms: int, scan_ms: int) -> dict:
    """Closed-only, ordered, contiguous, and the newest candle is the most recently closed one."""
    issues = []
    if not candles:
        return {"count": 0, "issues": ["no candles"]}
    if any(c.close_time >= scan_ms for c in candles):
        issues.append("contains an unclosed candle")
    gaps = sum(1 for a, b in zip(candles, candles[1:]) if b.open_time - a.open_time != span_ms)
    if gaps:
        issues.append(f"{gaps} gaps/irregular steps")
    expected_last_open = (scan_ms // span_ms) * span_ms - span_ms
    if candles[-1].open_time != expected_last_open:
        issues.append(f"last candle opens {utc(candles[-1].open_time)}, expected {utc(expected_last_open)}")
    bad_ohlc = sum(1 for c in candles if not (c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high))
    if bad_ohlc:
        issues.append(f"{bad_ohlc} candles with inconsistent OHLC")
    return {"count": len(candles), "first": utc(candles[0].open_time), "last": utc(candles[-1].open_time),
            "last_close": candles[-1].close, "issues": issues}


def check_ohlcv(module, limit: int) -> dict:
    out = {}
    for interval, span in (("1d", DAY_MS), ("4h", H4_MS)):
        try:
            scan = now_ms()
            candles = module.klines("BTCUSDT", interval, limit=limit)
            out[interval] = validate_candles(candles, span, scan)
            out[interval]["_candles"] = candles
        except Exception as exc:
            out[interval] = {"error": str(exc)[:300]}
    return out


def cross_check(a: dict, b: dict) -> dict:
    """Compare BTC daily closes on common dates between two sources."""
    ca, cb = a.get("1d", {}).get("_candles"), b.get("1d", {}).get("_candles")
    if not ca or not cb:
        return {"available": False}
    mb = {c.open_time: c.close for c in cb}
    diffs = [abs(c.close - mb[c.open_time]) / mb[c.open_time] * 100 for c in ca if c.open_time in mb]
    if not diffs:
        return {"available": False}
    return {"available": True, "common_days": len(diffs), "max_diff_pct": round(max(diffs), 4),
            "mean_diff_pct": round(sum(diffs) / len(diffs), 4), "within_0_2pct": max(diffs) <= 0.2}


def build_universe(module) -> dict:
    pairs = module.usdt_spot_symbols()
    all_bases = {b for _, b in pairs}
    vol24 = module.tickers_24h()
    excluded, candidates = {}, []
    for sym, base in pairs:
        reason = universe.exclusion_reason(base, all_bases)
        if reason:
            excluded[sym] = reason
        else:
            candidates.append((sym, base))
    # Only fetch 30d history for pairs with some 24h activity; the 30d median is the actual ranking key.
    prefilter = [(s, b) for s, b in candidates if vol24.get(s, 0) >= PREFILTER_24H_USD]
    entries, failures = [], {}
    for sym, base in prefilter:
        try:
            candles = module.klines(sym, "1d", limit=31)[-30:]
            entries.append(universe.UniverseEntry(sym, base, universe.median_quote_volume([c.quote_volume for c in candles]), len(candles)))
        except Exception as exc:
            failures[sym] = str(exc)[:200]
        time.sleep(0.12)
    ranked = universe.rank(entries)
    sensitivity = {f: len(universe.rank(entries, size=10_000, floor=f)) for f in FLOOR_SENSITIVITY}
    # Exchange metadata for everything above the lowest floor, so each listing's asset class can be
    # verified from the exchange's own data rather than guessed from the ticker.
    meta = {}
    if hasattr(module, "usdt_spot_metadata"):
        keep = {e.symbol for e in universe.rank(entries, size=10_000, floor=min(FLOOR_SENSITIVITY))}
        meta = {m["symbol"]: {k: v for k, v in m.items() if k not in ("filters", "orderTypes")}
                for m in module.usdt_spot_metadata() if m["symbol"] in keep}
    return {"pairs_total": len(pairs), "excluded": excluded, "prefiltered": len(prefilter),
            "fetch_failures": failures, "ranked": ranked, "floor_sensitivity": sensitivity,
            "all_entries": [e.__dict__ for e in sorted(entries, key=lambda e: -e.median_qvol_30d)],
            "symbol_metadata": meta}


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    env = {"runner": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
           "runner_os": platform.platform(), "hostname_hint": socket.gethostname()[:12],
           "github_run_id": os.environ.get("GITHUB_RUN_ID"), "started_utc": started.isoformat(timespec="seconds")}

    reach = check_reachability()
    ohlcv = {"binance_vision": check_ohlcv(binance, 1000) if reach["binance_vision"]["ok_rounds"] else {},
             "okx": check_ohlcv(okx, 300) if reach["okx"]["ok_rounds"] else {}}
    xcheck = cross_check(ohlcv["binance_vision"], ohlcv["okx"])

    def healthy(name):
        r, o = reach[name], ohlcv.get(name, {})
        return (r["ok_rounds"] == ROUNDS and all("error" not in o.get(i, {"error": 1}) and not o[i]["issues"]
                                                    for i in ("1d", "4h")))

    if healthy("binance_vision"):
        chosen, module, why = "binance_vision", binance, "Binance (data-api.binance.vision) נגיש ביציבות ונתוני BTC תקינים"
    elif healthy("okx"):
        chosen, module, why = "okx", okx, "Binance לא נגיש/לא תקין מסביבת ההרצה; OKX נגיש ונתוניו תקינים"
    else:
        chosen, module, why = None, None, "אף מקור לא עבר את הבדיקה — אין בחירה"

    uni = None
    if module is not None:
        try:
            uni = build_universe(module)
        except SourceError as exc:
            uni = {"error": str(exc)}

    # ---- write outputs ----
    run_tag = f"run-{env['github_run_id']}" if env["github_run_id"] else f"local-{started:%Y%m%dT%H%M%SZ}"
    reports = ROOT / "reports" / "phase0" / "runs" / run_tag
    reports.mkdir(parents=True, exist_ok=False)
    for src in ohlcv.values():
        for v in src.values():
            v.pop("_candles", None)

    universe_file = None
    if uni and "ranked" in uni:
        udir = ROOT / "data" / "universe"
        udir.mkdir(parents=True, exist_ok=True)
        universe_file = udir / f"universe-{started:%Y-%m-%d}-{run_tag}.csv"
        with universe_file.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["rank", "symbol", "base", "median_quote_volume_30d_usdt", "days", "source", "built_utc"])
            for i, e in enumerate(uni["ranked"], 1):
                w.writerow([i, e.symbol, e.base, round(e.median_qvol_30d), e.days, chosen, env["started_utc"]])

    result = {"env": env, "reachability": reach, "ohlcv_btc": ohlcv, "cross_check_btc_1d": xcheck,
              "decision": {"chosen_source": chosen, "reason_he": why},
              "universe": None if not uni else {
                  k: v for k, v in uni.items() if k != "ranked"} | (
                  {"size": len(uni["ranked"]), "file": str(universe_file.relative_to(ROOT))} if "ranked" in uni else {})}
    (reports / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    (reports / "REPORT.md").write_text(render_report(result, uni))
    print((reports / "REPORT.md").read_text())
    return 0 if chosen else 1


def render_report(r: dict, uni: dict | None) -> str:
    L = ["# דוח שלב 0 — בדיקת מקורות נתונים", "",
         f"- זמן הרצה: {r['env']['started_utc']} · סביבה: `{r['env']['runner']}` · run id: `{r['env']['github_run_id']}`", "",
         "## נגישות", "", "| מקור | הצלחות | סטטוס | זמן תגובה (ms) | שגיאה |", "|---|---|---|---|---|"]
    for name, v in r["reachability"].items():
        err = (v["errors"][0] if v["errors"] else "").replace("|", "/")[:120]
        L.append(f"| {name} | {v['ok_rounds']}/{v['rounds']} | {v['statuses']} | {v['latency_ms']} | {err} |")
    L += ["", "## נתוני BTCUSDT (נרות סגורים בלבד)", "", "| מקור | טווח | נרות | ראשון | אחרון | סגירה אחרונה | בעיות |", "|---|---|---|---|---|---|---|"]
    for src, d in r["ohlcv_btc"].items():
        if not d:
            L.append(f"| {src} | — | לא נבדק (לא נגיש) | | | | |")
        for itv, v in d.items():
            if "error" in v:
                L.append(f"| {src} | {itv} | שגיאה | | | | {v['error'][:100]} |")
            else:
                L.append(f"| {src} | {itv} | {v['count']} | {v['first']} | {v['last']} | {v['last_close']} | {'; '.join(v['issues']) or 'אין'} |")
    x = r["cross_check_btc_1d"]
    L += ["", "## התאמה בין מקורות (סגירה יומית של BTC)", ""]
    L.append(f"- ימים משותפים: {x['common_days']}, סטייה מקסימלית: {x['max_diff_pct']}%, ממוצעת: {x['mean_diff_pct']}%, בתוך 0.2%: {'כן' if x['within_0_2pct'] else 'לא'}"
             if x.get("available") else "- לא ניתן להשוות (אחד המקורות לא זמין)")
    L += ["", "## החלטה", "", f"- מקור נבחר: **{r['decision']['chosen_source'] or 'אין'}**", f"- נימוק: {r['decision']['reason_he']}", ""]
    if uni and "ranked" in uni:
        L += ["## יקום", "",
              f"- צמדי USDT פעילים: {uni['pairs_total']} · הוחרגו: {len(uni['excluded'])} · נבדקו לעומק: {uni['prefiltered']} · כשלי טעינה: {len(uni['fetch_failures'])}",
              f"- נבחרו: **{len(uni['ranked'])}** מטבעות (חציון מחזור 30 יום ≥ $5M)", "",
              "### רגישות לרצפת הנזילות", "", "| רצפה (חציון 30 יום) | מספר צמדים שעוברים |", "|---|---|"]
        L += [f"| ${f/1e6:.0f}M | {n} |" for f, n in uni["floor_sensitivity"].items()]
        L += ["", "### הוחרגו", "", ", ".join(f"{s} ({r})" for s, r in sorted(uni["excluded"].items())) or "—", "",
              "### נבחרו", "",
              "| # | מטבע | חציון מחזור יומי 30 יום (USDT) |", "|---|---|---|"]
        for i, e in enumerate(uni["ranked"], 1):
            L.append(f"| {i} | {e.base} | {e.median_qvol_30d:,.0f} |")
    elif uni:
        L += ["## יקום", "", f"- שגיאה בבניית היקום: {uni.get('error')}"]
    L += ["", "> כל הנתונים בדוח נמשכו בזמן ההרצה מה-API הציבורי. אין בדוח נתונים מדומים."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
