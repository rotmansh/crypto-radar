"""Offline unit tests. All candle values here are synthetic TEST FIXTURES, never shown to users."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from radar import universe  # noqa: E402
from radar.sources import base, binance, okx  # noqa: E402
from radar.sources.base import Candle  # noqa: E402

DAY = 86_400_000


def fixture_candle(i, close=100.0):
    return Candle(i * DAY, (i + 1) * DAY - 1, close, close + 1, close - 1, close, 1.0, close)


def test_drop_unclosed_removes_open_candle():
    candles = [fixture_candle(i) for i in range(3)]
    now = 2 * DAY + 5  # inside candle 2
    assert [c.open_time for c in base.drop_unclosed(candles, now)] == [0, DAY]


def test_binance_parser_drops_open_candle(monkeypatch):
    now = base.now_ms()
    day0 = (now // DAY) * DAY
    rows = [[day0 - DAY, "1", "2", "0.5", "1.5", "10", day0 - 1, "15", 0, 0, 0, 0],
            [day0, "1.5", "2", "1", "1.8", "3", day0 + DAY - 1, "5", 0, 0, 0, 0]]
    monkeypatch.setattr(binance, "get_json", lambda *a, **k: (rows, 200, 1.0))
    out = binance.klines("BTCUSDT", "1d")
    assert len(out) == 1 and out[0].close == 1.5 and out[0].quote_volume == 15


def test_okx_parser_uses_confirm_flag_and_sorts(monkeypatch):
    now = base.now_ms()
    day0 = (now // DAY) * DAY
    payload = {"code": "0", "data": [
        [str(day0), "1", "2", "0.5", "1.5", "10", "15", "15", "0"],          # open (confirm=0)
        [str(day0 - DAY), "1", "2", "0.5", "1.4", "10", "14", "14", "1"],
        [str(day0 - 2 * DAY), "1", "2", "0.5", "1.3", "10", "13", "13", "1"],
    ]}
    calls = {}
    monkeypatch.setattr(okx, "get_json", lambda url, params, **k: (calls.update(params) or payload, 200, 1.0))
    out = okx.klines("BTCUSDT", "1d")
    assert calls["bar"] == "1Dutc" and calls["instId"] == "BTC-USDT"
    assert [c.close for c in out] == [1.3, 1.4]


def test_exclusions():
    bases = {"BTC", "BTCUP", "JUP", "SYRUP", "USDC", "WBTC", "ETH"}
    assert universe.exclusion_reason("USDC", bases) == "stable_or_pegged"
    assert universe.exclusion_reason("WBTC", bases) == "wrapped"
    assert universe.exclusion_reason("BTCUP", bases) == "leveraged_token"
    assert universe.exclusion_reason("JUP", bases) is None
    assert universe.exclusion_reason("SYRUP", bases) is None
    assert universe.exclusion_reason("ETH", bases) is None


def test_rank_applies_floor_and_size():
    E = universe.UniverseEntry
    entries = [E("A", "A", 9e6, 30), E("B", "B", 4e6, 30), E("C", "C", 2e7, 30), E("D", "D", 3e7, 10)]
    assert [e.symbol for e in universe.rank(entries, size=5)] == ["C", "A"]


def test_validate_candles_flags_gaps_and_staleness():
    import phase0_probe as p
    candles = [fixture_candle(i) for i in (0, 1, 3)]
    res = p.validate_candles(candles, DAY, 5 * DAY + 10)
    assert any("gaps" in i for i in res["issues"])
    assert any("expected" in i for i in res["issues"])
    ok = p.validate_candles([fixture_candle(i) for i in range(5)], DAY, 5 * DAY + 10)
    assert ok["issues"] == []


def test_okx_4h_uses_supported_bar(monkeypatch):
    calls = {}
    monkeypatch.setattr(okx, "get_json", lambda url, params, **k: (calls.update(params) or {"code": "0", "data": []}, 200, 1.0))
    okx.klines("BTCUSDT", "4h")
    assert calls["bar"] == "4H"
