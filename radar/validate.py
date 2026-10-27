"""Integrity checks for a candle series. Problems are reported, never repaired (no interpolation)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .sources.base import Candle
from .store import iso


@dataclass
class SeriesReport:
    count: int = 0
    first: str | None = None
    last: str | None = None
    duplicates: int = 0
    unordered: int = 0
    unclosed: int = 0
    misaligned: int = 0
    bad_ohlc: int = 0
    gaps: list[dict] = field(default_factory=list)  # [{after, before, missing_candles}]

    @property
    def fatal(self) -> bool:
        """Defects that mean the stored data itself is wrong (gaps can be genuine exchange outages)."""
        return bool(self.duplicates or self.unordered or self.unclosed or self.misaligned or self.bad_ohlc)


def validate_series(candles: list[Candle], span_ms: int, now_ms: int) -> SeriesReport:
    r = SeriesReport(count=len(candles))
    if not candles:
        return r
    r.first, r.last = iso(candles[0].open_time), iso(candles[-1].open_time)
    seen: set[int] = set()
    for c in candles:
        if c.open_time in seen:
            r.duplicates += 1
        seen.add(c.open_time)
        if c.close_time >= now_ms:
            r.unclosed += 1
        if c.open_time % span_ms != 0 or c.close_time != c.open_time + span_ms - 1:
            r.misaligned += 1
        if not (c.low <= min(c.open, c.close) and max(c.open, c.close) <= c.high and c.low >= 0):
            r.bad_ohlc += 1
    for a, b in zip(candles, candles[1:]):
        step = b.open_time - a.open_time
        if step <= 0:
            r.unordered += 1
        elif step != span_ms:
            r.gaps.append({"after": iso(a.open_time), "before": iso(b.open_time),
                           "missing_candles": step // span_ms - 1})
    return r
