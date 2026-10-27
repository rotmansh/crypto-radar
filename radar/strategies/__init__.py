"""The three independent strategies (SPEC §3). Each is versioned; changing a parameter requires a new version
(enforced by tests/test_strategies.py::test_params_are_pinned_to_versions)."""
from .base import INSUFFICIENT, NONE, SIGNAL, WATCH, Strategy  # noqa: F401
from .bollinger_squeeze import BollingerSqueeze
from .sma50_pullback import SMA50Pullback
from .trend_rs import TrendRelativeStrength, rs_panel  # noqa: F401

ALL: tuple[Strategy, ...] = (SMA50Pullback(), BollingerSqueeze(), TrendRelativeStrength())
BY_ID = {s.id: s for s in ALL}
