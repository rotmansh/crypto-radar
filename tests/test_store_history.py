import dataclasses

from conftest import DAY, H4, make_candles

from radar import history
from radar.store import CandleStore
from radar.validate import validate_series

JAN_2024 = 1_704_067_200_000  # 2024-01-01T00:00Z


class FakeExchange:
    """Pages a TEST FIXTURE series like the Binance klines endpoint (startTime inclusive, limit, drops unclosed)."""

    def __init__(self, candles, page_size=1000):
        self.candles, self.page_size, self.calls = candles, page_size, 0

    def fetch(self, symbol, interval, start):
        self.calls += 1
        return [c for c in self.candles if c.open_time >= start][: self.page_size]


def test_store_roundtrip_across_months(tmp_path):
    s = CandleStore(tmp_path, "binance")
    candles = make_candles(70, DAY, JAN_2024)
    s.append("XUSDT", "1d", candles)
    files = sorted(p.name for p in (tmp_path / "binance/1d/XUSDT").glob("*.csv"))
    assert files == ["2024-01.csv", "2024-02.csv", "2024-03.csv"]
    assert s.load("XUSDT", "1d") == candles


def test_full_paged_sync_then_incremental(tmp_path):
    all_c = make_candles(2600, H4, JAN_2024)
    now = all_c[2500].close_time  # candle 2500 is still open at "now"
    ex = FakeExchange(all_c[:2501], page_size=1000)
    s = CandleStore(tmp_path, "binance")

    r = history.sync(s, ex.fetch, "XUSDT", "4h", H4, now, overlap=3, page_size=1000)
    assert r.error is None and r.appended == 2500 and r.pages == 3
    stored = s.load("XUSDT", "4h")
    assert stored == all_c[:2500]  # open candle never stored
    assert validate_series(stored, H4, now).fatal is False

    # Nothing new: idempotent, no duplicates
    r2 = history.sync(s, ex.fetch, "XUSDT", "4h", H4, now, overlap=3)
    assert r2.appended == 0 and len(s.load("XUSDT", "4h")) == 2500

    # Time passes: 50 more candles close
    ex.candles = all_c[:2551]
    later = all_c[2550].close_time
    r3 = history.sync(s, ex.fetch, "XUSDT", "4h", H4, later, overlap=3)
    assert r3.appended == 50 and s.load("XUSDT", "4h") == all_c[:2550]


def test_revision_is_logged_not_overwritten(tmp_path):
    all_c = make_candles(100, DAY, JAN_2024)
    s = CandleStore(tmp_path, "binance")
    s.append("XUSDT", "1d", all_c[:90])
    revised = list(all_c)
    revised[89] = dataclasses.replace(all_c[89], close=all_c[89].close * 1.01)
    r = history.sync(s, FakeExchange(revised).fetch, "XUSDT", "1d", DAY, all_c[99].close_time + 1, overlap=3)
    assert len(r.revisions) == 1 and r.appended == 10
    assert s.load("XUSDT", "1d")[89] == all_c[89]  # stored value kept


def test_gap_is_reported_not_filled(tmp_path):
    all_c = make_candles(50, DAY, JAN_2024)
    with_gap = all_c[:20] + all_c[23:]  # exchange outage: 3 candles missing
    s = CandleStore(tmp_path, "binance")
    r = history.sync(s, FakeExchange(with_gap).fetch, "XUSDT", "1d", DAY, all_c[-1].close_time + 1)
    assert r.error is None and r.report.gaps == [{"after": "2024-01-20T00:00:00Z", "before": "2024-01-24T00:00:00Z", "missing_candles": 3}]
    assert len(s.load("XUSDT", "1d")) == 47


def test_invalid_data_is_not_written(tmp_path):
    all_c = make_candles(30, DAY, JAN_2024)
    bad = list(all_c)
    bad[10] = dataclasses.replace(bad[10], high=bad[10].low * 0.5)  # high < low
    s = CandleStore(tmp_path, "binance")
    r = history.sync(s, FakeExchange(bad).fetch, "XUSDT", "1d", DAY, all_c[-1].close_time + 1)
    assert r.error and r.appended == 0 and s.load("XUSDT", "1d") == []


def test_validate_flags_duplicates_misalignment_unclosed():
    c = make_candles(5, DAY, JAN_2024)
    rep = validate_series(c + [c[-1]], DAY, c[-1].close_time + 1)
    assert rep.duplicates == 1 and rep.fatal
    shifted = [dataclasses.replace(x, open_time=x.open_time + 1, close_time=x.close_time + 1) for x in c]
    assert validate_series(shifted, DAY, 10**15).misaligned == 5
    assert validate_series(c, DAY, c[-1].close_time).unclosed == 1
