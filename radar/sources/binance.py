"""Binance Spot public market data (https://developers.binance.com/docs/binance-spot-api-docs).

Read-only market-data endpoints on data-api.binance.vision. No keys, no account or order endpoints.
"""
from __future__ import annotations

from .base import Candle, SourceError, drop_unclosed, get_json, now_ms

NAME = "binance"
BASE_URL = "https://data-api.binance.vision"
INTERVALS = {"1d": "1d", "4h": "4h"}
INTERVAL_MS = {"1d": 86_400_000, "4h": 14_400_000}
MAX_KLINES = 1000


def klines(symbol: str, interval: str, limit: int = MAX_KLINES, start_ms: int | None = None,
           base_url: str = BASE_URL) -> list[Candle]:
    """Closed candles only, oldest first. With `start_ms`, returns candles opening at or after it."""
    params = {"symbol": symbol, "interval": INTERVALS[interval], "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    data, _, _ = get_json(f"{base_url}/api/v3/klines", params)
    if not isinstance(data, list):
        raise SourceError(f"unexpected klines payload for {symbol}: {str(data)[:200]}")
    candles = [
        Candle(
            open_time=int(k[0]),
            close_time=int(k[6]),
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5]),
            quote_volume=float(k[7]),
        )
        for k in data
    ]
    return drop_unclosed(candles, now_ms())


def exchange_symbols(base_url: str = BASE_URL) -> list[dict]:
    """Raw exchangeInfo entries for every spot symbol, any status (used for dated listing snapshots)."""
    data, _, _ = get_json(f"{base_url}/api/v3/exchangeInfo", {"permissions": "SPOT"})
    return data["symbols"]


def usdt_spot_metadata(base_url: str = BASE_URL) -> list[dict]:
    """Raw exchangeInfo entries for USDT spot pairs currently TRADING."""
    return [
        s for s in exchange_symbols(base_url)
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s.get("isSpotTradingAllowed", True)
    ]


def usdt_spot_symbols(base_url: str = BASE_URL) -> list[tuple[str, str]]:
    """[(symbol, base_asset)] for USDT spot pairs currently TRADING."""
    return [(s["symbol"], s["baseAsset"]) for s in usdt_spot_metadata(base_url)]


def tickers_24h(base_url: str = BASE_URL) -> dict[str, float]:
    """{symbol: 24h quote volume in USDT}."""
    data, _, _ = get_json(f"{base_url}/api/v3/ticker/24hr", {"type": "MINI"})
    return {t["symbol"]: float(t["quoteVolume"]) for t in data}


def order_book(symbol: str, limit: int = 500, base_url: str = BASE_URL) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(bids, asks) as [(price, qty)], best first."""
    data, _, _ = get_json(f"{base_url}/api/v3/depth", {"symbol": symbol, "limit": limit})
    bids = [(float(p), float(q)) for p, q in data["bids"]]
    asks = [(float(p), float(q)) for p, q in data["asks"]]
    return bids, asks


def book_spread_pct(symbol: str, base_url: str = BASE_URL) -> float | None:
    data, _, _ = get_json(f"{base_url}/api/v3/ticker/bookTicker", {"symbol": symbol})
    bid, ask = float(data["bidPrice"]), float(data["askPrice"])
    if bid <= 0 or ask <= 0:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100
