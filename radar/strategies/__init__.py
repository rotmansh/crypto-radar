"""The three independent strategies (SPEC §3). Each is versioned; changing a parameter requires a new version
(enforced by tests/test_strategies.py::test_params_are_pinned_to_versions)."""
from .base import INSUFFICIENT, NONE, SIGNAL, WATCH, Strategy  # noqa: F401
from .bollinger_squeeze import BollingerSqueeze
from .sma50_pullback import SMA50Pullback, SMA50PullbackTail
from .trend_rs import TrendRelativeStrength, rs_panel  # noqa: F401

ALL: tuple[Strategy, ...] = (SMA50Pullback(), BollingerSqueeze(), TrendRelativeStrength())
BY_ID = {s.id: s for s in ALL}
# Research variants: never run by the live scan or shown on the dashboard; evaluated in phase 5 (SPEC §3.1.1).
RESEARCH: tuple[Strategy, ...] = tuple(SMA50PullbackTail(v) for v in SMA50PullbackTail.TAIL_K)
# Strategy D (docs/STRATEGY_D_SPEC.md): research only — six management versions, one entry rule set.
from .sma50_swing import SMA50Swing  # noqa: E402

SWING_RESEARCH: tuple[Strategy, ...] = tuple(SMA50Swing(v) for v in SMA50Swing.MANAGEMENT)
