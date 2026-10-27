"""Shared strategy engine.

A strategy declares numbered rules. Each rule is a column of 1.0 (met), 0.0 (not met) or NaN (not enough data to
decide). The engine turns the rule columns into one state per closed candle:

  SIGNAL        every signal rule is met (confirmed, after the confirmation candle closed)
  WATCH         every watch rule is met, but the signal is not — a setup forming, NOT an entry
  NONE          neither
  INSUFFICIENT  at least one signal rule cannot be evaluated (too little history); nothing is emitted

A rule that lacks data is never treated as met or as failed. All rule columns are causal: the value at row t uses
rows <= t only (tests/test_strategies.py::test_no_lookahead_*).
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

SIGNAL, WATCH, NONE, INSUFFICIENT = "SIGNAL", "WATCH", "NONE", "INSUFFICIENT"


def flag(mask: pd.Series, *deps: pd.Series) -> pd.Series:
    """Boolean mask -> 1.0/0.0, NaN wherever any dependency is NaN (so missing data never reads as 'False')."""
    out = mask.astype(float)
    for d in deps:
        out[d.isna()] = np.nan
    return out


def fmt(x: float | None) -> str:
    """Prices and levels: 6 significant digits, thousands separators, never scientific notation."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if abs(x) >= 1:
        return f"{x:,.6g}" if abs(x) < 1e6 else f"{x:,.0f}"
    return np.format_float_positional(x, precision=4, fractional=False, trim="-")


def fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:.2f}"


@dataclass(frozen=True)
class Rule:
    key: str
    label_he: str
    detail: Callable[[pd.Series], str]  # row -> Hebrew text with the measured values


class Strategy:
    id: str
    version: str
    name_he: str
    timeframes: tuple[str, ...]
    params: dict
    min_candles: int  # candles needed before every rule can be evaluated
    rules: tuple[Rule, ...]
    watch_rules: tuple[str, ...]
    signal_rules: tuple[str, ...]
    cooldown: int = 10  # no new SIGNAL for this many candles after one (SPEC §3.0)

    @property
    def params_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.params, sort_keys=True).encode()).hexdigest()[:12]

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def conditions(self, df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
        """Return c_<rule> columns plus stop / target (NaN where undefined) and any values used in explanations."""
        raise NotImplementedError

    def evaluate(self, df: pd.DataFrame, ctx: dict | None = None) -> pd.DataFrame:
        """Rule columns plus `state` and `suppressed` for every candle of an indicator frame."""
        cond = self.conditions(df, ctx or {})
        sig = cond[[f"c_{k}" for k in self.signal_rules]]
        wat = cond[[f"c_{k}" for k in self.watch_rules]]
        known = sig.notna().all(axis=1).to_numpy()
        sig_ok = (sig == 1).all(axis=1).to_numpy() & known
        wat_ok = (wat == 1).all(axis=1).to_numpy() & wat.notna().all(axis=1).to_numpy()
        states, suppressed, last_sig = [], [], -10**9
        for i in range(len(cond)):
            sup = False
            if not known[i]:
                st = INSUFFICIENT
            elif sig_ok[i] and i - last_sig > self.cooldown:
                st, last_sig = SIGNAL, i
            elif sig_ok[i]:
                st, sup = NONE, True  # confirmed again inside the cooldown window: not re-issued
            elif wat_ok[i]:
                st = WATCH
            else:
                st = NONE
            states.append(st)
            suppressed.append(sup)
        cond["state"] = states
        cond["suppressed_by_cooldown"] = suppressed
        cond["rr"] = np.where((cond["target"] > df["close"]) & (df["close"] > cond["stop"]),
                              (cond["target"] - df["close"]) / (df["close"] - cond["stop"]), np.nan)
        return cond

    def watch_invalidation(self, row: pd.Series) -> tuple[float, str]:
        """For a WATCH: the level whose break cancels the setup. A WATCH has no entry, stop, target or R:R."""
        raise NotImplementedError

    def explain(self, row: pd.Series) -> list[dict]:
        out = []
        for r in self.rules:
            v = row[f"c_{r.key}"]
            out.append({"rule": r.key, "label_he": r.label_he,
                        "met": None if pd.isna(v) else bool(v == 1),
                        "in_watch": r.key in self.watch_rules, "in_signal": r.key in self.signal_rules,
                        "detail_he": r.detail(row)})
        return out

    def reason_he(self, row: pd.Series) -> str:
        keys = self.signal_rules if row["state"] == SIGNAL else self.watch_rules
        parts = [r.detail(row) for r in self.rules if r.key in keys]
        if row["state"] == WATCH:
            missing = [r.label_he for r in self.rules if r.key in self.signal_rules and row[f"c_{r.key}"] != 1]
            parts.append("חסר לאישור: " + ", ".join(missing))
        return " · ".join(parts)
