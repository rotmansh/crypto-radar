"""Load system settings and the explicit exclusion list from config/."""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.toml"
EXCLUSIONS_PATH = ROOT / "config" / "exclusions.toml"
REVIEW_PATH = ROOT / "config" / "review.toml"

REQUIRED = {
    "data": ["source", "base_url", "quote_asset", "intervals", "store_dir", "sync_overlap_candles"],
    "universe": ["min_median_quote_volume_usd", "max_size", "volume_lookback_days", "min_history_candles",
                 "prefilter_24h_quote_volume_usd", "tradability_days"],
    "asset_checks": ["common_permission_share", "stable_max_range_pct", "weekend_volume_ratio_min"],
    "liquidity": ["depth_limit", "depth_bands_pct", "impact_sizes_usd", "max_spread_pct", "min_depth_1pct_usd",
                  "suspend_window", "suspend_min_hits", "suspend_min_span_hours"],
}


class ConfigError(ValueError):
    pass


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    with open(path, "rb") as f:
        settings = tomllib.load(f)
    missing = [f"{sec}.{key}" for sec, keys in REQUIRED.items() for key in keys if key not in settings.get(sec, {})]
    if missing:
        raise ConfigError(f"missing settings: {', '.join(missing)}")
    if settings["data"]["source"] != "binance":
        raise ConfigError("only the 'binance' source is implemented; see docs/DATA_SOURCES.md before changing it")
    return settings


def load_exclusions(path: Path = EXCLUSIONS_PATH) -> dict[str, dict]:
    """{base_asset: {category, reason, added, evidence}}."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    out: dict[str, dict] = {}
    for category, entry in raw.items():
        for field in ("reason", "added", "evidence", "bases"):
            if field not in entry:
                raise ConfigError(f"exclusions.{category} is missing '{field}'")
        for base in entry["bases"]:
            if base in out:
                raise ConfigError(f"{base} is listed twice ({out[base]['category']}, {category})")
            out[base] = {"category": category, "reason": entry["reason"], "added": entry["added"],
                         "evidence": entry["evidence"]}
    return out


def load_review(path: Path = REVIEW_PATH) -> dict:
    """{"verified": {base: {...}}, "hold": {base: {...}}}"""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    out = {"verified": raw.get("verified", {}), "hold": raw.get("hold", {})}
    for base, v in out["verified"].items():
        for field in ("accepted_flags", "decided", "evidence"):
            if field not in v:
                raise ConfigError(f"review.verified.{base} is missing '{field}'")
    for base, v in out["hold"].items():
        for field in ("decided", "question"):
            if field not in v:
                raise ConfigError(f"review.hold.{base} is missing '{field}'")
    both = set(out["verified"]) & set(out["hold"])
    if both:
        raise ConfigError(f"both verified and on hold: {sorted(both)}")
    return out


def fingerprint(obj: object) -> str:
    """Short stable hash of a settings section, stored with outputs so results can be traced to their config."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]
