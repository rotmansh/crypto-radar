"""Incremental, paged download of full candle history into the CandleStore.

- First sync starts at the beginning of the pair's history (startTime=0) and pages forward 1000 at a time.
- Later syncs re-fetch the last `overlap` stored candles: any difference is logged as a revision and the
  stored value is kept (history is append-only; a revision is a finding to investigate, not to overwrite).
- Only closed candles are ever stored; duplicates are impossible because only candles newer than the last
  stored one are appended.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .sources.base import Candle
from .store import CandleStore, iso
from .validate import SeriesReport, validate_series

FetchPage = Callable[[str, str, int], list[Candle]]  # (symbol, interval, start_ms) -> closed candles, oldest first


@dataclass
class SyncResult:
    symbol: str
    interval: str
    pages: int = 0
    appended: int = 0
    revisions: list[dict] = field(default_factory=list)
    report: SeriesReport | None = None
    error: str | None = None


def _differs(a: Candle, b: Candle) -> bool:
    return (a.open, a.high, a.low, a.close, a.volume, a.quote_volume, a.close_time) != (
        b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.close_time)


def sync(store: CandleStore, fetch_page: FetchPage, symbol: str, interval: str, span_ms: int,
         now_ms: int, overlap: int = 3, page_size: int = 1000, max_pages: int = 1000) -> SyncResult:
    res = SyncResult(symbol, interval)
    stored = store.load(symbol, interval)
    by_time = {c.open_time: c for c in stored[-overlap:]} if stored else {}
    last_stored = stored[-1].open_time if stored else None
    start = stored[-overlap].open_time if len(stored) >= overlap else (stored[0].open_time if stored else 0)

    new: list[Candle] = []
    while res.pages < max_pages:
        page = [c for c in fetch_page(symbol, interval, start) if c.close_time < now_ms]
        res.pages += 1
        if not page:
            break
        for c in page:
            if last_stored is not None and c.open_time <= last_stored:
                old = by_time.get(c.open_time)
                if old is not None and _differs(old, c):
                    res.revisions.append({"open_time": iso(c.open_time), "stored": old.__dict__, "fetched": c.__dict__})
                continue
            if new and c.open_time <= new[-1].open_time:
                continue  # page overlap — never store a candle twice
            new.append(c)
        if len(page) < page_size:
            break
        start = page[-1].open_time + span_ms
    else:
        res.error = f"stopped after {max_pages} pages"

    res.report = validate_series(stored + new, span_ms, now_ms)
    if res.report.fatal:
        res.error = "validation failed — nothing written"
        return res
    res.appended = store.append(symbol, interval, new)
    return res
