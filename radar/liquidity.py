"""Order-book liquidity metrics from a single depth snapshot: spread, depth within bands, price impact.

These are point-in-time measurements of the live book. Historical books are not available from the
public API, so backtests cannot use them (they use the tiered slippage model in SPEC §5 instead).
"""
from __future__ import annotations

Level = tuple[float, float]  # (price, quantity)


def _impact_pct(levels: list[Level], size_usd: float, mid: float) -> float | None:
    """Average fill price vs mid, in %, for a market order of `size_usd` walking `levels`.
    None when the visible book is too thin to fill it (reported as 'not computable', never estimated)."""
    remaining, cost, qty = size_usd, 0.0, 0.0
    for price, q in levels:
        take = min(remaining, price * q)
        cost += take
        qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            return abs(cost / qty - mid) / mid * 100
    return None


def book_metrics(bids: list[Level], asks: list[Level], bands_pct: list[float], sizes_usd: list[float]) -> dict:
    if not bids or not asks:
        return {"error": "empty order book"}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    out: dict = {"mid": mid, "spread_pct": (best_ask - best_bid) / mid * 100,
                 "book_bid_floor_pct": (mid - bids[-1][0]) / mid * 100,
                 "book_ask_ceiling_pct": (asks[-1][0] - mid) / mid * 100}
    for band in bands_pct:
        lo, hi = mid * (1 - band / 100), mid * (1 + band / 100)
        out[f"depth_bid_{band}pct_usd"] = sum(p * q for p, q in bids if p >= lo)
        out[f"depth_ask_{band}pct_usd"] = sum(p * q for p, q in asks if p <= hi)
        # If the fetched levels stop before the band edge the figure is only a lower bound (deep books, e.g. BTC).
        out[f"depth_bid_{band}pct_lower_bound"] = bids[-1][0] > lo
        out[f"depth_ask_{band}pct_lower_bound"] = asks[-1][0] < hi
    for size in sizes_usd:
        out[f"impact_buy_{int(size)}_pct"] = _impact_pct(asks, size, mid)
        out[f"impact_sell_{int(size)}_pct"] = _impact_pct(bids, size, mid)
    return out


def liquidity_flags(m: dict, max_spread_pct: float, min_depth_1pct_usd: float) -> list[str]:
    if "error" in m:
        return ["order_book_unavailable"]
    flags = []
    if m["spread_pct"] > max_spread_pct:
        flags.append("wide_spread")
    for side in ("bid", "ask"):
        d = m.get(f"depth_{side}_1.0pct_usd")
        if d is not None and d < min_depth_1pct_usd and not m.get(f"depth_{side}_1.0pct_lower_bound"):
            flags.append(f"thin_book_{side}")
    return flags
