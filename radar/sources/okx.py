"""OKX Spot public market data (https://www.okx.com/docs-v5/en/).

Note: OKX's plain `1D` bar is aligned to Hong Kong time (UTC+8); we use `1Dutc` / `4Hutc`
so candles close on the same UTC boundaries as Binance.
"""
from __future__ import annotations

from .base import Candle, SourceError, drop_unclosed, get_json, now_ms

NAME = "okx"
BASE_URL = "https://www.okx.com"
BARS = {"1d": ("1Dutc", 86_400_000), "4h": ("4Hutc", 14_400_000)}


def _inst_id(symbol: str) -> str:
    # 'BTCUSDT' -> 'BTC-USDT'
    if not symbol.endswith("USDT"):
        raise ValueError(symbol)
    return f"{symbol[:-4]}-USDT"


def klines(symbol: str, interval: str, limit: int = 300) -> list[Candle]:
    """Closed candles only, oldest first. Max 300 per request on this endpoint."""
    bar, span = BARS[interval]
    data, _, _ = get_json(
        f"{BASE_URL}/api/v5/market/candles",
        {"instId": _inst_id(symbol), "bar": bar, "limit": min(limit, 300)},
    )
    if not isinstance(data, dict) or data.get("code") != "0":
        raise SourceError(f"unexpected OKX payload for {symbol}: {str(data)[:200]}")
    candles = []
    for row in data["data"]:
        if row[8] != "1":  # confirm flag: 1 = closed
            continue
        ts = int(row[0])
        candles.append(
            Candle(
                open_time=ts,
                close_time=ts + span - 1,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                quote_volume=float(row[7]),
            )
        )
    candles.sort(key=lambda c: c.open_time)
    return drop_unclosed(candles, now_ms())


def usdt_spot_symbols() -> list[tuple[str, str]]:
    data, _, _ = get_json(f"{BASE_URL}/api/v5/public/instruments", {"instType": "SPOT"})
    return [
        (i["baseCcy"] + "USDT", i["baseCcy"])
        for i in data["data"]
        if i["quoteCcy"] == "USDT" and i["state"] == "live"
    ]


def tickers_24h() -> dict[str, float]:
    """{symbol: 24h quote volume in USDT}."""
    data, _, _ = get_json(f"{BASE_URL}/api/v5/market/tickers", {"instType": "SPOT"})
    out = {}
    for t in data["data"]:
        if t["instId"].endswith("-USDT"):
            out[t["instId"].replace("-", "")] = float(t["volCcy24h"])
    return out


def book_spread_pct(symbol: str) -> float | None:
    data, _, _ = get_json(f"{BASE_URL}/api/v5/market/ticker", {"instId": _inst_id(symbol)})
    t = data["data"][0]
    bid, ask = float(t["bidPx"] or 0), float(t["askPx"] or 0)
    if bid <= 0 or ask <= 0:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 100
