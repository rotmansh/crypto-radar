"""Universe selection: ~100 liquid USDT spot pairs ranked by 30-day median daily quote volume."""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# Stablecoins and fiat/gold-pegged tokens: no meaningful trend to analyse.
STABLE_OR_PEGGED = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "USDE", "USDS", "PYUSD", "USD1", "RLUSD",
    "USDD", "FRAX", "LUSD", "GUSD", "SUSD", "USTC", "UST", "EURI", "EUR", "AEUR", "EURC", "EURQ",
    "TRY", "BRL", "GBP", "AUD", "PAXG", "XAUT", "BFUSD", "XUSD", "USDG", "USDQ",
}
# Wrapped / staked derivatives that track another asset in the universe.
WRAPPED = {"WBTC", "WBETH", "WETH", "BETH", "STETH", "WSTETH", "CBBTC", "BTCB", "SOLVBTC", "BNSOL"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

MIN_MEDIAN_QVOL_USD = 5_000_000
TARGET_SIZE = 100


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    base: str
    median_qvol_30d: float
    days: int


def exclusion_reason(base: str, all_bases: set[str]) -> str | None:
    if base in STABLE_OR_PEGGED:
        return "stable_or_pegged"
    if base in WRAPPED:
        return "wrapped"
    # A leveraged token is <underlying><suffix> where the underlying is itself listed (e.g. BTCUP -> BTC).
    # Matching on the suffix alone would wrongly drop real coins such as JUP or SYRUP.
    for suffix in LEVERAGED_SUFFIXES:
        if base.endswith(suffix) and base[: -len(suffix)] in all_bases:
            return "leveraged_token"
    return None


def median_quote_volume(daily_quote_volumes: list[float]) -> float:
    return statistics.median(daily_quote_volumes) if daily_quote_volumes else 0.0


def rank(entries: list[UniverseEntry], size: int = TARGET_SIZE, floor: float = MIN_MEDIAN_QVOL_USD) -> list[UniverseEntry]:
    eligible = [e for e in entries if e.median_qvol_30d >= floor and e.days >= 30]
    eligible.sort(key=lambda e: e.median_qvol_30d, reverse=True)
    return eligible[:size]
