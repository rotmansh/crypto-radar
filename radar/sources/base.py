from __future__ import annotations

import time
from dataclasses import dataclass

import requests

USER_AGENT = "crypto-radar/0.1 (+https://github.com/rotmansh/crypto-radar)"
TIMEOUT = 20


@dataclass(frozen=True)
class Candle:
    open_time: int  # ms since epoch, UTC
    close_time: int  # ms since epoch, UTC (last ms of the candle)
    open: float
    high: float
    low: float
    close: float
    volume: float  # base asset
    quote_volume: float  # USDT


class SourceError(RuntimeError):
    pass


def get_json(url: str, params: dict | None = None, retries: int = 3) -> tuple[object, int, float]:
    """GET a JSON document. Returns (payload, http_status, latency_ms). Retries on network errors and 5xx/429."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        t0 = time.monotonic()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            latency = (time.monotonic() - t0) * 1000
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = SourceError(f"HTTP {r.status_code} from {url}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise SourceError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
            return r.json(), r.status_code, latency
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(2 ** attempt)
    raise SourceError(f"failed after {retries} attempts: {last_exc}")


def drop_unclosed(candles: list[Candle], now_ms: int) -> list[Candle]:
    """Keep only candles whose close_time is strictly before `now_ms`."""
    return [c for c in candles if c.close_time < now_ms]


def now_ms() -> int:
    return int(time.time() * 1000)
