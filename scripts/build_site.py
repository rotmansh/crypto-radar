"""Build the static dashboard (site/) from stored data. Nothing here computes strategy logic of its own:

  - live view   <- data/scan/latest.json (scripts/scan.py, shared engine)
  - charts      <- candles + indicator columns from radar.indicators.compute_all (causal)
  - Time Machine<- states/items from radar.engine.run over the full history, split by period

Self-check before publishing: for several as_of points (always including 2025-03-15) a true replay
(radar.replay, candles truncated at as_of) is compared with the precomputed timeline. Any mismatch fails the build.
The site's data gateway (web/tm-data.js) only ever hands the UI rows at or before as_of in Replay mode.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from radar import config, engine, replay  # noqa: E402
from radar.present import clean, item  # noqa: E402
from radar.store import CandleStore, iso  # noqa: E402
from radar.strategies import ALL, SIGNAL, WATCH  # noqa: E402
from scan import NOT_PROOF_HE, SURVIVORSHIP_HE, code_commit  # noqa: E402

OUT = ROOT / "site"
SELF_CHECK = [("1d", "2025-03-15T00:00:00Z"), ("4h", "2025-03-15T00:00:00Z"), ("1d", "2021-05-20T00:00:00Z"),
              ("4h", "2023-10-24T08:00:00Z"), ("1d", "2026-10-01T00:00:00Z")]


def chunk_of(ms: int, tf: str) -> str:
    d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
    return f"{d:%Y}" if tf == "1d" else f"{d:%Y-%m}"


def r8(x):
    v = clean(x)
    return None if v is None else float(f"{v:.8g}")


def write(path: Path, obj) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text)
    return len(text.encode())


def journal_rows() -> list[dict]:
    rows = []
    for name, version in (("signals.csv", "v1"), ("signals-v2.csv", "v2")):
        p = ROOT / "journal" / name
        if p.exists():
            with p.open(newline="") as f:
                for r in csv.DictReader(f):
                    r["journal_version"] = version
                    if version == "v1":  # every v1 row was written on the alert's first candle by a live scan
                        r["origin"] = "live_new"
                        r["episode_started_utc"] = r["candle_open_utc"]
                    rows.append(r)
    return rows


def main() -> int:
    settings = config.load_settings()
    exclusions, review = config.load_exclusions(), config.load_review()
    store = CandleStore(ROOT / settings["data"]["store_dir"], settings["data"]["source"])
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(ROOT / "web", OUT)
    data = OUT / "data"
    sizes = defaultdict(int)

    # --- live view
    scan = json.loads((ROOT / "data/scan/latest.json").read_text())
    sizes["scan"] += write(data / "scan.json", scan)
    sizes["journal"] += write(data / "journal.json", journal_rows())
    snap_path = sorted((ROOT / "data/universe/snapshots").glob("*.csv"))[-1]
    with snap_path.open(newline="") as f:
        snap = [r for r in csv.DictReader(f) if r["status"] in ("included", "review", "liquidity_suspended")]
    sizes["universe"] += write(data / "universe.json", {"snapshot": snap_path.name, "rows": snap})

    # --- system health (written by scripts/health.py at the end of every run, including failed runs)
    hdir = ROOT / "data/health"
    health = json.loads((hdir / "latest.json").read_text()) if (hdir / "latest.json").exists() else None
    sizes["health"] += write(data / "health.json", health)

    def tail_csv(path: Path, n: int) -> list[dict]:
        if not path.exists():
            return []
        with path.open(newline="") as f:
            return list(csv.DictReader(f))[-n:]
    sizes["health"] += write(data / "health_runs.json", tail_csv(hdir / "runs.csv", 60))
    sizes["health"] += write(data / "site_checks.json", tail_csv(hdir / "site-checks.csv", 20))
    sizes["health"] += write(data / "gate_decisions.json", tail_csv(hdir / "gate-decisions.csv", 30))
    outbox = sum(len(p.read_text().splitlines()) for p in (ROOT / "data/outbox").glob("*.jsonl")) \
        if (ROOT / "data/outbox").exists() else 0

    # --- full-history engine run (same engine as scan and replay)
    pool = replay.candidate_pool(store, exclusions, review)
    raw = {tf: engine.load_candles(store, pool, tf) for tf in settings["data"]["intervals"]}
    res = engine.run(raw, settings["universe"], set(pool), ALL)

    # candles + indicator columns per symbol, chunked (1d: year, 4h: month)
    index = {}
    for tf, frames in res.frames.items():
        for sym, df in frames.items():
            chunks = defaultdict(list)
            cols = [df[c].to_numpy() for c in ("open_time", "open", "high", "low", "close", "volume", "sma50",
                                               "sma200", "bb_upper", "bb_mid", "bb_lower", "partial")]
            c_ev = res.evaluated.get(("C-TRS", "1d"), {}).get(sym) if tf == "1d" else None
            rs = c_ev["rs"].to_numpy() if c_ev is not None else None
            for i in range(len(df)):
                t = int(cols[0][i])
                row = [t // 1000] + [r8(c[i]) for c in cols[1:11]] + [int(bool(cols[11][i]))]
                if tf == "1d":
                    row.append(r8(rs[i]) if rs is not None else None)  # RS = close / BTC close (engine)
                chunks[chunk_of(t, tf)].append(row)
            for ch, rows in chunks.items():
                sizes["candles"] += write(data / "candles" / tf / sym / f"{ch}.json", rows)
            index.setdefault(sym, {})[tf] = {"chunks": sorted(chunks), "first": int(df["open_time"].iloc[0]) // 1000,
                                             "last": int(df["open_time"].iloc[-1]) // 1000}

    # Time Machine timeline: SIGNAL/WATCH items of member candles + daily membership, chunked like candles
    tm = defaultdict(lambda: {"entries": [], "members": {}})
    for (sid, tf), per in res.evaluated.items():
        strat = next(s for s in ALL if s.id == sid)
        for sym, ev in per.items():
            df = res.frames[tf][sym]
            states = ev["state"].to_numpy()
            for i in np.flatnonzero((states == SIGNAL) | (states == WATCH)):
                it = item(strat, sym, tf, df, ev, int(i), {}, settings["risk"]["high_risk_stop_pct"])
                t = int(df["open_time"].iloc[i])
                ep = int(df["open_time"].iloc[engine.episode_start(ev["state"], int(i))])
                rules_met = "".join("-" if r["met"] is None else ("1" if r["met"] else "0") for r in it["rules"])
                tm[(tf, chunk_of(t, tf))]["entries"].append({
                    "t": t // 1000, "s": sym, "st": sid, "k": "S" if states[i] == SIGNAL else "W", "ep": ep // 1000,
                    "p": it["ref_price"], "stop": it["stop"], "tgt": it["target"], "rr": it["rr"],
                    "risk": it["risk_pct_to_stop"], "hr": it["high_risk"], "inv": it["watch_invalidation"],
                    "inv_he": it["watch_invalidation_he"], "bl": it["breakout_level"], "atr": it["atr_pct"],
                    "rv": it["realized_vol_30"], "pw": it["partial_in_window"], "why": it["reason_he"],
                    "rm": rules_met, "rr_note": it["rr_note"]})
    for t_day, row in res.membership.iterrows():
        ms = int(t_day.value // 10**6)
        members = sorted(s for s, v in row.items() if v)
        for tf in settings["data"]["intervals"]:
            # a day's membership applies to candles that close after that day's close
            key = chunk_of(ms + engine.DAY_MS, tf) if tf == "4h" else chunk_of(ms, tf)
            tm[(tf, key)]["members"][str(ms // 1000)] = members
    # per-coin history of reconstructed alerts (SIGNALs and the first candle of each WATCH episode)
    hist = defaultdict(list)
    for (tf, ch), body in tm.items():
        for e in body["entries"]:
            if e["k"] == "S" or e["ep"] == e["t"]:
                hist[e["s"]].append({"tf": tf, **{k: e[k] for k in ("t", "st", "k", "p", "stop", "tgt", "rr", "risk", "hr", "inv", "bl")}})
    for sym, rows in hist.items():
        rows.sort(key=lambda r: (r["t"], r["st"]))
        sizes["history"] += write(data / "history" / f"{sym}.json", rows)
    tm_index = defaultdict(list)
    for (tf, ch), body in sorted(tm.items()):
        body["entries"].sort(key=lambda e: (e["t"], e["st"], e["s"]))
        sizes["timemachine"] += write(data / "tm" / tf / f"{ch}.json", body)
        tm_index[tf].append(ch)

    # --- self-check: precomputed timeline == true truncated replay
    checks = []
    for tf, as_of in SELF_CHECK:
        ms = replay.parse_utc(as_of)
        r = replay.replay(store, settings, exclusions, review, ms, tf)
        got = {(x["symbol"], b["strategy"], x["state"]) for b in r["results"] if b["available"]
               for x in b["signals"] + b["watch"]}
        last = replay.parse_utc(r["last_available_candle_utc"]) // 1000
        body = tm.get((tf, chunk_of(last * 1000, tf)), {"entries": []})
        want = {(e["s"], e["st"], "SIGNAL" if e["k"] == "S" else "WATCH") for e in body["entries"] if e["t"] == last}
        ok = got == want
        checks.append({"timeframe": tf, "as_of_utc": as_of, "replay_items": len(got), "timeline_items": len(want),
                       "match": ok, "data_fingerprint": r["data_fingerprint"]})
        if not ok:
            print("SELF-CHECK MISMATCH", tf, as_of, sorted(got ^ want)[:10])
            return 1

    strategies = [{"id": s.id, "version": s.version, "ref": s.ref, "params_hash": s.params_hash, "name_he": s.name_he,
                   "timeframes": list(s.timeframes), "min_candles": s.min_candles, "params": s.params,
                   "rules": [{"key": r.key, "label_he": r.label_he, "watch": r.key in s.watch_rules,
                              "signal": r.key in s.signal_rules} for r in s.rules]} for s in ALL]
    meta = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "code_commit": code_commit(), "source": settings["data"]["source"],
            "scan_run": scan["run"], "scan_generated_utc": scan["generated_utc"],
            "timeline_fingerprint": res.data_fingerprint, "candidate_pool": pool,
            "strategies": strategies, "high_risk_stop_pct": settings["risk"]["high_risk_stop_pct"],
            "candles_index": index, "tm_index": tm_index, "self_check": checks,
            "limits_he": replay.LIMITS_HE, "survivorship_he": SURVIVORSHIP_HE, "not_proof_he": NOT_PROOF_HE,
            "stale_after_hours": 6, "review_holds": review.get("hold", {}),
            "review_suspended": review.get("suspended", {}),
            "review_verified": {k: {"decided": v["decided"], "accepted_flags": v["accepted_flags"]}
                                for k, v in review.get("verified", {}).items()},
            "outbox_messages": outbox, "notifications_enabled": settings.get("notifications", {}).get("enabled", False),
            "schedule_he": "כל 4 שעות, 23 דקות אחרי סגירת נר 4H (00:23, 04:23 … 20:23 UTC), ובנוסף גיבוי שבודק כל שעה. GitHub עשוי לעכב או לדלג על ריצות מתוזמנות."}
    sizes["meta"] += write(data / "meta.json", meta)
    print("site built:", {k: f"{v / 1e6:.1f} MB" for k, v in sizes.items()}, "self-check:",
          [(c["timeframe"], c["as_of_utc"], c["replay_items"], c["match"]) for c in checks])
    return 0


if __name__ == "__main__":
    sys.exit(main())
