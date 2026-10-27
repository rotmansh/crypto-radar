"""Time Machine replay from the command line.

    python scripts/replay.py --as-of 2025-03-15T00:00:00Z --tf 1d --strategy all --save

Prints the replay (Replay mode only: nothing after --as-of). With --save, writes it to
data/replays/<as_of>_<tf>_<strategies>_<data_fingerprint>.json — kept apart from the live journal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import config, replay  # noqa: E402
from radar.store import CandleStore  # noqa: E402
from radar.strategies import ALL  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))
from scan import code_commit  # noqa: E402

LETTERS = {"A": "A-SMA50PB", "B": "B-BBSQ", "C": "C-TRS"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default="2025-03-15T00:00:00Z")
    ap.add_argument("--tf", default="1d", choices=["1d", "4h"])
    ap.add_argument("--strategy", default="all", help="all, or letters such as A or BC")
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    ids = None if a.strategy == "all" else tuple(LETTERS[x] for x in a.strategy.upper())
    settings = config.load_settings()
    store = CandleStore(ROOT / settings["data"]["store_dir"], settings["data"]["source"])
    r = replay.replay(store, settings, config.load_exclusions(), config.load_review(), replay.parse_utc(a.as_of),
                      a.tf, ids, code_commit())
    text = json.dumps(r, ensure_ascii=False, indent=1)
    if a.save:
        names = "all" if ids is None else "".join(k for k, v in LETTERS.items() if v in ids)
        out = ROOT / "data/replays" / f"{r['as_of_utc'].replace(':', '')}_{a.tf}_{names}_{r['data_fingerprint']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"saved {out.relative_to(ROOT)}")
    for b in r["results"]:
        if b["available"]:
            print(b["strategy"], "SIGNAL:", [x["base"] for x in b["signals"]], "WATCH:", [x["base"] for x in b["watch"]])
    print("last available candle:", r["last_available_candle_utc"], "| universe:", len(r["universe_at_as_of"]),
          "| fingerprint:", r["data_fingerprint"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
