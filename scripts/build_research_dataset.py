"""Build the phase 5a research dataset from the official Binance archive — IN-SAMPLE ONLY (candles closed by
2025-01-01T00:00Z; no 2025 file is ever requested).

  python scripts/build_research_dataset.py [--limit N]

Steps (docs/PHASE5_PLAN.md §1–2):
  1. list every spot symbol in the archive; keep USDT pairs that are not excluded by asset type or leveraged tokens
  2. download all monthly 1d files up to 2024-12, verify official checksums, validate each series
  3. split series at gaps > 7 days (ticker reuse, e.g. LUNA) into separate series SYMBOL#1, SYMBOL#2, ...
  4. point-in-time universe from the daily data -> coins that were ever members
  5. download 4h files for those coins only
  6. cross-check archive candles against the live store (API) for every pair both hold
Outputs: data/research_cache/is_{1d,4h}.pkl (not committed) and reports/backtest/dataset/<stamp>/ (committed):
MANIFEST.csv (every file + sha256), DATASET.md, dataset.json.
"""
from __future__ import annotations

import argparse
import dataclasses
import csv
import datetime as dt
import json
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from radar import archive, config, indicators, validate  # noqa: E402
from radar.backtest import IS_END_MS, LEVERAGED_SUFFIXES, research_membership  # noqa: E402
from radar.store import CandleStore  # noqa: E402

CACHE = ROOT / "data/research_cache"
LAST_MONTH = "2024-12"
SPAN = indicators.INTERVAL_MS
SPLIT_GAP_DAYS = 7
WORKERS = 16
CLOSE_TIME_FIXES: dict[str, int] = {}
TRAILING_ZERO_TRIMMED: dict[str, int] = {}


def usdt_candidates(symbols: list[str], exclusions: dict) -> tuple[list[str], dict[str, str]]:
    keep, dropped = [], {}
    for s in symbols:
        if not s.endswith("USDT") or len(s) <= 4:
            continue
        base = s[:-4]
        if base in exclusions:
            dropped[s] = "excluded:" + exclusions[base]["category"]
        elif base.endswith(LEVERAGED_SUFFIXES):
            dropped[s] = "excluded:leveraged_token"
        else:
            keep.append(s)
    return keep, dropped


def segments(candles: list, interval: str) -> list[list]:
    """Split at gaps longer than SPLIT_GAP_DAYS (a reused ticker or a long suspension starts a new series)."""
    out, cur = [], []
    for c in candles:
        if cur and c.open_time - cur[-1].open_time > SPLIT_GAP_DAYS * 86_400_000:
            out.append(cur)
            cur = []
        cur.append(c)
    if cur:
        out.append(cur)
    return out


def download(symbols: list[str], interval: str, manifest: list[dict]) -> tuple[dict, dict]:
    """symbol -> candles (<= LAST_MONTH, deduplicated, ordered); problems per symbol."""
    def months(s):
        return s, [m for m in archive.list_months(s, interval) if m <= LAST_MONTH]
    with ThreadPoolExecutor(WORKERS) as ex:
        month_map = dict(ex.map(months, symbols))
    jobs = [(s, m) for s, ms in month_map.items() for m in ms]

    def get(job):
        s, m = job
        path, sha = archive.fetch(s, interval, m, CACHE / "zips")
        return s, m, path, sha
    with ThreadPoolExecutor(WORKERS) as ex:
        files = list(ex.map(get, jobs))
    by_sym: dict[str, list] = {}
    for s, m, path, sha in sorted(files):
        manifest.append({"symbol": s, "interval": interval, "month": m, "file": path.name, "sha256": sha})
        by_sym.setdefault(s, []).extend(archive.parse(path))
    out, problems = {}, {}
    for s, candles in by_sym.items():
        candles = sorted({c.open_time: c for c in candles}.values(), key=lambda c: c.open_time)
        # Archive quirk: a few files end a candle 1 ms late (close_time = open_time + span). Normalised and counted.
        fixed = 0
        for k, c in enumerate(candles):
            if c.close_time == c.open_time + SPAN[interval]:
                candles[k] = dataclasses.replace(c, close_time=c.close_time - 1)
                fixed += 1
        if fixed:
            CLOSE_TIME_FIXES[f"{s}/{interval}"] = fixed
        # After a delisting some files carry flat zero-volume rows; trailing ones are not trading data. Trimmed, counted.
        k = len(candles)
        while k and candles[k - 1].volume == 0:
            k -= 1
        if k < len(candles):
            TRAILING_ZERO_TRIMMED[f"{s}/{interval}"] = len(candles) - k
            candles = candles[:k]
        candles = [c for c in candles if c.close_time < IS_END_MS]
        rep = validate.validate_series(candles, SPAN[interval], IS_END_MS)
        if rep.fatal:
            problems[s] = {k: v for k, v in rep.__dict__.items() if k not in ("examples", "gaps")}
            continue
        if candles:
            out[s] = (candles, rep)
    return out, problems


def cross_check(frames_1d: dict, store: CandleStore) -> dict:
    """Compare archive daily candles with the live store's API candles on every shared (symbol, open time)."""
    rows = cmp = mism = vol_only = 0
    worst = []
    for s in store.symbols("1d"):
        if s not in frames_1d:
            continue
        api = {c.open_time: c for c in store.load(s, "1d") if c.close_time < IS_END_MS}
        for c in frames_1d[s]:
            a = api.get(c.open_time)
            rows += 1
            if a is None:
                continue
            cmp += 1
            rel = {f: abs(getattr(a, f) - getattr(c, f)) / max(abs(getattr(a, f)), 1e-12)
                   for f in ("open", "high", "low", "close", "volume")}
            diff = max(rel[f] for f in ("open", "high", "low", "close"))
            if diff <= 1e-9 and rel["volume"] > 1e-9:
                vol_only += 1
            if diff > 1e-9:
                mism += 1
                if len(worst) < 10:
                    worst.append({"symbol": s, "open_time": c.open_time, "max_rel_diff": diff})
    return {"archive_rows_in_shared_symbols": rows, "compared": cmp, "price_mismatched": mism,
            "volume_only_differences": vol_only, "examples": worst}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="first N candidates only (smoke test)")
    a = ap.parse_args(argv)
    settings, exclusions = config.load_settings(), config.load_exclusions()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ")
    out_dir = ROOT / "reports/backtest/dataset" / stamp
    manifest: list[dict] = []

    all_syms = archive.list_symbols()
    cands, dropped = usdt_candidates(all_syms, exclusions)
    if a.limit:
        cands = sorted(set(cands[: a.limit]) | {"BTCUSDT"})
    print(f"archive symbols {len(all_syms)} · USDT candidates {len(cands)} · excluded {len(dropped)}", flush=True)

    daily, prob_1d = download(cands, "1d", manifest)
    frames_1d, seg_info = {}, {}
    for s, (candles, rep) in daily.items():
        segs = segments(candles, "1d")
        if len(segs) == 1:
            frames_1d[s] = segs[0]
        else:
            for k, seg in enumerate(segs, 1):
                frames_1d[f"{s}#{k}"] = seg
            seg_info[s] = [{"series": f"{s}#{k}", "first": seg[0].open_time, "last": seg[-1].open_time}
                           for k, seg in enumerate(segs, 1)]
    raw_1d = {s: indicators.to_frame(c) for s, c in frames_1d.items()}
    member = research_membership(raw_1d, settings["universe"])
    ever = sorted(member.columns[member.any(axis=0)])
    print(f"daily series {len(raw_1d)} · ever members {len(ever)}", flush=True)

    base_syms = sorted({s.split("#")[0] for s in ever})
    h4, prob_4h = download(base_syms, "4h", manifest)
    raw_4h = {}
    for s, (candles, rep) in h4.items():
        segs = segments(candles, "4h")
        names = [s] if len(segs) == 1 else [f"{s}#{k}" for k in range(1, len(segs) + 1)]
        for name, seg in zip(names, segs):
            if name in ever:
                raw_4h[name] = indicators.to_frame(seg)

    store = CandleStore(ROOT / settings["data"]["store_dir"], settings["data"]["source"])
    xc = cross_check(frames_1d, store)
    delisted = sorted(s for s in {x.split("#")[0] for x in ever} if s not in set(store.symbols("1d")))

    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "is_1d.pkl", "wb") as f:
        pickle.dump(raw_1d, f)  # every daily series: the universe needs all candidates
    with open(CACHE / "is_4h.pkl", "wb") as f:
        pickle.dump(raw_4h, f)

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "MANIFEST.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "interval", "month", "file", "sha256"])
        w.writeheader()
        w.writerows(manifest)
    summary = {
        "built_utc": stamp, "is_end_utc": "2025-01-01T00:00:00Z", "last_month": LAST_MONTH,
        "archive_symbols": len(all_syms), "usdt_candidates": len(cands), "excluded": dropped,
        "daily_series": len(raw_1d), "split_series": seg_info, "ever_members": len(ever),
        "ever_members_not_in_live_store": len(delisted), "fourh_series": len(raw_4h),
        "rejected_1d": prob_1d, "rejected_4h": prob_4h, "close_time_fixes": CLOSE_TIME_FIXES,
        "trailing_zero_volume_trimmed": TRAILING_ZERO_TRIMMED, "parse_quirks": archive.QUIRKS, "files": len(manifest), "cross_check_vs_api": xc,
        "gaps_1d": {s: rep.gaps for s, (c, rep) in daily.items() if rep.gaps},
    }
    (out_dir / "dataset.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k not in ("excluded", "split_series", "gaps_1d")},
                     default=str, indent=1))
    print("dataset report:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
