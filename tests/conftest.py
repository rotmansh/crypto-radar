"""Shared TEST FIXTURES. Synthetic candles only — never written to data/ and never shown to users."""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from radar.sources.base import Candle  # noqa: E402

DAY = 86_400_000
H4 = 14_400_000


def make_candles(n, span=DAY, start=0, seed=1, start_price=100.0):
    rng = random.Random(seed)
    out, price = [], start_price
    for i in range(n):
        o = price
        c = max(0.01, o * (1 + rng.gauss(0, 0.03)))
        h, l = max(o, c) * (1 + abs(rng.gauss(0, 0.01))), min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        t = start + i * span
        v = rng.uniform(100, 1000)
        out.append(Candle(t, t + span - 1, o, h, l, c, v, v * c))
        price = c
    return out
