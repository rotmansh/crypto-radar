"""Universe classification: which USDT pairs are scanned, excluded, or held for review.

Membership depends on volume, asset type, listing age and tradability. Order-book liquidity is reported as a
warning and can only suspend a pair through a persistence rule over many measurements (liquidity.persistence).

Every candidate gets exactly one status:
  included              scanned (may carry liquidity warnings)
  review                an asset-type flag without a still-valid verification, a manual hold, or a failed
                        tradability check; NOT scanned until a person decides (config/review.toml)
  liquidity_suspended   persistent liquidity problem over time; NOT scanned, re-evaluated every run
  excluded              on the explicit exclusion list (config/exclusions.toml)
  below_floor           30d median daily quote volume under the floor
  below_prefilter       24h volume too low to be worth measuring
  short_volume_history  listed for fewer than `volume_lookback_days` closed daily candles
  overflow              eligible but beyond `max_size`

No single undocumented exchange field decides the outcome on its own: a flag only routes a pair to review.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .sources.base import Candle

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


@dataclass
class Candidate:
    symbol: str
    base: str
    status: str = ""
    median_qvol_30d: float | None = None
    days: int = 0
    flags: list[str] = field(default_factory=list)  # asset-type flags only
    exclusion: dict | None = None
    liquidity_warnings: list[str] = field(default_factory=list)
    liquidity_suspended: bool = False
    tradable: bool | None = None
    review_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)  # measured values behind each check, kept for the snapshot


def leveraged_token(base: str, all_bases: set[str]) -> bool:
    # <underlying><suffix> where the underlying is itself listed (BTCUP -> BTC); suffix alone would hit JUP, SYRUP.
    return any(base.endswith(s) and base[: -len(s)] in all_bases for s in LEVERAGED_SUFFIXES)


def common_permission_groups(metadata: list[dict], share: float) -> set[str]:
    """Permission groups present on at least `share` of all listed pairs."""
    counts: Counter[str] = Counter()
    for m in metadata:
        counts.update({g for ps in m.get("permissionSets", []) for g in ps})
    return {g for g, n in counts.items() if n >= share * len(metadata)}


def permission_anomaly(meta: dict, common: set[str]) -> list[str]:
    groups = {g for ps in meta.get("permissionSets", []) for g in ps}
    return sorted(common - groups)


def weekend_volume_ratio(candles: list[Candle]) -> float | None:
    """Median Sat/Sun daily quote volume divided by median Mon–Fri volume (UTC days)."""
    wkend, wkday = [], []
    for c in candles:
        (wkend if dt.datetime.fromtimestamp(c.open_time / 1000, dt.timezone.utc).weekday() >= 5 else wkday).append(c.quote_volume)
    if len(wkend) < 4 or len(wkday) < 10 or statistics.median(wkday) <= 0:
        return None
    return statistics.median(wkend) / statistics.median(wkday)


def price_range_pct(candles: list[Candle]) -> float | None:
    closes = [c.close for c in candles]
    return (max(closes) / min(closes) - 1) * 100 if closes and min(closes) > 0 else None


def asset_flags(candles: list[Candle], meta: dict | None, common_groups: set[str], checks_cfg: dict) -> tuple[list[str], dict]:
    flags, values = [], {}
    if meta is not None:
        missing = permission_anomaly(meta, common_groups)
        values["missing_common_permission_groups"] = missing
        if missing:
            flags.append("permission_profile_anomaly")
    rng = price_range_pct(candles)
    values["range_30d_pct"] = rng
    if rng is not None and rng < checks_cfg["stable_max_range_pct"]:
        flags.append("stable_like_price")
    ratio = weekend_volume_ratio(candles)
    values["weekend_volume_ratio"] = ratio
    if ratio is not None and ratio < checks_cfg["weekend_volume_ratio_min"]:
        flags.append("equity_like_weekend_volume")
    return flags, values


def tradability(candles: list[Candle], book_ok: bool, days: int) -> tuple[bool, dict]:
    """Can the pair actually be traded now? Listed as TRADING with spot allowed (filtered upstream), both sides
    of the order book present, and volume on each of the last `days` daily candles."""
    recent = candles[-days:]
    zero = [c.open_time for c in recent if c.volume <= 0]
    ok = book_ok and len(recent) >= days and not zero
    return ok, {"book_both_sides": book_ok, "days_checked": len(recent), "zero_volume_days": len(zero)}


def apply_review(c: Candidate, review: dict) -> str | None:
    """Return a review reason, or None if the pair may proceed. Mutates c.notes for verified pairs."""
    if c.base in review.get("hold", {}):
        return "manual_hold: " + review["hold"][c.base]["question"]
    s = review.get("suspended", {}).get(c.base)
    if s and s["lift_when"] == "asset_verified_and_liquidity_ok" and c.base not in review.get("verified", {}):
        return f"suspended_pending_asset_verification ({s['decided']}): {s['reason']}"
    if c.tradable is False:
        return "not_tradable"
    if c.flags:
        v = review.get("verified", {}).get(c.base)
        if v and set(c.flags) <= set(v["accepted_flags"]):
            c.notes.append(f"verified {v['decided']}: accepted {', '.join(c.flags)}")
            return None
        return "asset_flags: " + ", ".join(c.flags)
    return None


def finalize(candidates: list[Candidate], floor: float, max_size: int, lookback_days: int,
             review: dict | None = None) -> list[Candidate]:
    """Assign statuses to candidates that do not have one yet. Flags, tradability and liquidity must be set."""
    review = review or {}
    pool = []
    for c in candidates:
        if c.status:
            continue
        if c.days < lookback_days:
            c.status = "short_volume_history"
        elif (c.median_qvol_30d or 0) < floor:
            c.status = "below_floor"
        elif reason := apply_review(c, review):
            c.status, c.review_reason = "review", reason
        elif c.liquidity_suspended:
            c.status = "liquidity_suspended"
            if s := review.get("suspended", {}).get(c.base):
                c.notes.append(f"suspended {s['decided']}: {s['reason']}; lifts automatically when the liquidity rule clears")
        else:
            if s := review.get("suspended", {}).get(c.base):
                c.notes.append(f"suspension of {s['decided']} lifted: liquidity rule no longer holds")
            pool.append(c)
    pool.sort(key=lambda c: c.median_qvol_30d, reverse=True)
    for i, c in enumerate(pool):
        c.status = "included" if i < max_size else "overflow"
    return candidates
