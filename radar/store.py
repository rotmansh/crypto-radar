"""On-disk OHLCV store: one CSV per symbol, interval and calendar month (UTC).

  data/ohlcv/<source>/<interval>/<SYMBOL>/<YYYY-MM>.csv

Closed months never change; each sync only appends to the current month's file, which keeps git diffs
small and makes the append-only check (scripts/check_append_only.py) meaningful.
"""
from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path

from .sources.base import Candle

COLUMNS = ["open_time_ms", "open_time_utc", "open", "high", "low", "close", "volume", "quote_volume", "close_time_ms"]


def _month(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m")


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CandleStore:
    def __init__(self, root: Path, source: str):
        self.root = Path(root) / source

    def _dir(self, symbol: str, interval: str) -> Path:
        return self.root / interval / symbol

    def symbols(self, interval: str) -> list[str]:
        d = self.root / interval
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def load(self, symbol: str, interval: str) -> list[Candle]:
        d = self._dir(symbol, interval)
        if not d.exists():
            return []
        out: list[Candle] = []
        for path in sorted(d.glob("*.csv")):
            with path.open(newline="") as f:
                for row in csv.DictReader(f):
                    out.append(Candle(
                        open_time=int(row["open_time_ms"]), close_time=int(row["close_time_ms"]),
                        open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                        close=float(row["close"]), volume=float(row["volume"]),
                        quote_volume=float(row["quote_volume"]),
                    ))
        return out

    def append(self, symbol: str, interval: str, candles: list[Candle]) -> int:
        """Append candles (must all be newer than what is stored). Returns the number written."""
        if not candles:
            return 0
        d = self._dir(symbol, interval)
        d.mkdir(parents=True, exist_ok=True)
        by_month: dict[str, list[Candle]] = defaultdict(list)
        for c in candles:
            by_month[_month(c.open_time)].append(c)
        for month, rows in sorted(by_month.items()):
            path = d / f"{month}.csv"
            new_file = not path.exists()
            with path.open("a", newline="") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(COLUMNS)
                for c in rows:
                    w.writerow([c.open_time, iso(c.open_time), repr(c.open), repr(c.high), repr(c.low),
                                repr(c.close), repr(c.volume), repr(c.quote_volume), c.close_time])
        return len(candles)
