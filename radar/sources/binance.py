"""Binance Spot public market data (https://developers.binance.com/docs/binance-spot-api-docs)."""
from __future__ import annotations

from .base import Candle, SourceError, drop_unclosed, get_json, now_ms

NAME = "binance"
BASE_URL = "https://data-api.binance.vision"
INTERVALS = {"1d": "1d", "4h": "4h"}


def klines(symbol: str, interval: str, limit: int = 1000, base_url: str = BASE_URL) -> list[Candle]:
    """Closed candles only, oldest first. `symbol` like 'BTCUSDT'."""
    data, _, _ = get_json(
        f"{base_url}/api/v3/klines",
        {"symbol": symbol, "interval": INTERVALS[interval], "limit": limit},
    )
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


def usdt_spot_symbols(base_url: str = BASE_URL) -> list[tuple[str, str]]:
    """[(symbol, base_asset)] for USDT spot pairs currently TRADING."""
    data, _, _ = get_json(f"{base_url}/api/v3/exchangeInfo", {"permissions": "SPOT"})
    out = []
    for s in data["symbols"]:
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s.get("isSpotTradingAllowed", True):
            out.append((s["symbol"], s["baseAsset"]))
    return out


def tickers_24h(base_url: str = BASE_URL) -> dict[str, float]:
    """{symbol: 24h quote volume in USDT}."""
    data, _, _ = get_json(f"{base_url}/api/v3/ticker/24hr", {"type": "MINI"})
    return {t["symbol"]: float(t["quoteVolume"]) for t in data}


def book_spread_pct(symbol: str, base_url: str = BASE_URL) -> float | None:
    data, _, _ = get_json(f"{base_url}/api/v3/ticker/bookTicker", {"symbol": symbol})
    bid, ask = float(data["bidPrice"]), float(data["askPrice"])
    if bid <= 0 or ask <= 0:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100
