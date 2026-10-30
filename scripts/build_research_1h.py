"""1H research data for strategy D — IN-SAMPLE ONLY (docs/STRATEGY_D_SPEC.md §4, §7).

  python scripts/build_research_1h.py        # needs data/research_cache/is_1d.pkl (build_research_dataset.py)

Coins: every daily series that, on some day from 2019-10-01 on, was a point-in-time universe member with a 30-day
median $ volume >= $20M. Months 2019-10 .. 2024-12 (2019-10 .. 2020-02 only warm up indicators). Files are
checksum-verified and listed in a manifest; series split at gaps > 7 days get the name of the daily series whose
time range they fall in (e.g. LUNAUSDT#1), so 1h, 4h and daily always refer to the same asset.
Outputs: data/research_cache/is_1h.pkl (not committed), reports/backtest/dataset_1h/<stamp>/ (committed).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

import build_research_dataset as base  # noqa: E402
from radar import archive, config, indicators  # noqa: E402
from radar.backtest import median_volume, research_membership  # noqa: E402

FIRST_MONTH = "2019-10"
MIN_LIQUIDITY = 20_000_000
SINCE = pd.Timestamp("2019-10-01", tz="UTC")


def main() -> int:
    ucfg = config.load_settings()["universe"]
    with open(base.CACHE / "is_1d.pkl", "rb") as f:
        raw_1d = pickle.load(f)
    member = research_membership(raw_1d, ucfg)
    med = median_volume(raw_1d, ucfg["volume_lookback_days"])
    ok = (member & (med.reindex_like(member) >= MIN_LIQUIDITY))
    ok = ok[ok.index >= SINCE]
    series = sorted(ok.columns[ok.any(axis=0)])
    bases = sorted({s.split("#")[0] for s in series})
    print(f"1h coins: {len(series)} series / {len(bases)} symbols", flush=True)

    manifest: list[dict] = []
    got, problems = base.download(bases, "1h", manifest, FIRST_MONTH)
    ranges = {s: (int(raw_1d[s]["open_time"].iloc[0]), int(raw_1d[s]["open_time"].iloc[-1]) + indicators.INTERVAL_MS["1d"])
              for s in series}
    raw_1h, unmatched = {}, []
    for sym, (candles, rep) in got.items():
        for seg in base.segments(candles, "1h"):
            t0 = seg[0].open_time
            name = next((s for s in series if s.split("#")[0] == sym and ranges[s][0] <= t0 < ranges[s][1]), None)
            if name is None:
                unmatched.append(f"{sym}@{t0}")
                continue
            raw_1h[name] = indicators.to_frame(seg)
    with open(base.CACHE / "is_1h.pkl", "wb") as f:
        pickle.dump(raw_1h, f)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = ROOT / "reports/backtest/dataset_1h" / stamp
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "MANIFEST.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "interval", "month", "file", "sha256"])
        w.writeheader()
        w.writerows(manifest)
    summary = {"built_utc": stamp, "first_month": FIRST_MONTH, "last_month": base.LAST_MONTH,
               "min_liquidity_usd": MIN_LIQUIDITY, "eligible_series": len(series), "symbols": len(bases),
               "series_1h": len(raw_1h), "files": len(manifest), "rejected": problems,
               "unmatched_segments": unmatched, "close_time_fixes": base.CLOSE_TIME_FIXES,
               "trailing_zero_volume_trimmed": base.TRAILING_ZERO_TRIMMED, "parse_quirks": archive.QUIRKS,
               "gaps": {s: rep.gaps for s, (c, rep) in got.items() if rep.gaps}}
    (out / "dataset.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k != "gaps"}, default=str, indent=1)[:3000])
    print("dataset report:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
