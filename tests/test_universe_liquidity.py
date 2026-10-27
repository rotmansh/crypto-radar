import dataclasses

from conftest import DAY, make_candles

from radar import config, liquidity, universe
from radar.universe import Candidate

CHECKS = {"stable_max_range_pct": 3.0, "weekend_volume_ratio_min": 0.35}
MON_2024_01_01 = 1_704_067_200_000


def test_config_loads_and_floor_is_configurable():
    s = config.load_settings()
    assert s["universe"]["min_median_quote_volume_usd"] == 5_000_000
    assert s["data"]["base_url"] == "https://data-api.binance.vision"


def test_exclusions_file():
    ex = config.load_exclusions()
    assert ex["U"]["category"] == "unidentified"
    assert sum(1 for v in ex.values() if v["category"] == "tokenized_equity") == 21
    assert ex["USDC"]["category"] == "stable_or_pegged" and "BTC" not in ex


def test_exclusions_reject_duplicates(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text('[a]\nreason="r"\nadded="d"\nevidence="e"\nbases=["X"]\n[b]\nreason="r"\nadded="d"\nevidence="e"\nbases=["X"]\n')
    try:
        config.load_exclusions(p)
        assert False
    except config.ConfigError:
        pass


def test_leveraged_token_needs_listed_underlying():
    bases = {"BTC", "BTCUP", "JUP", "SYRUP"}
    assert universe.leveraged_token("BTCUP", bases)
    assert not universe.leveraged_token("JUP", bases) and not universe.leveraged_token("SYRUP", bases)


def test_permission_anomaly_is_generic_not_a_hardcoded_group():
    meta = [{"symbol": f"S{i}", "permissionSets": [["SPOT", "GRP_A", "GRP_B"]]} for i in range(99)]
    odd = {"symbol": "ODD", "permissionSets": [["SPOT", "GRP_A"]]}
    common = universe.common_permission_groups(meta + [odd], 0.95)
    assert common == {"SPOT", "GRP_A", "GRP_B"}
    assert universe.permission_anomaly(odd, common) == ["GRP_B"]
    assert universe.permission_anomaly(meta[0], common) == []


def test_stable_like_and_weekend_flags():
    flat = [dataclasses.replace(c, close=1.0 + (i % 3) * 0.001) for i, c in enumerate(make_candles(30, DAY, MON_2024_01_01))]
    flags, vals = universe.asset_flags(flat, None, set(), CHECKS)
    assert "stable_like_price" in flags and vals["range_30d_pct"] < 1
    equity = [dataclasses.replace(c, quote_volume=1e6 if (i % 7) < 5 else 1e5)
              for i, c in enumerate(make_candles(30, DAY, MON_2024_01_01, seed=3))]
    flags, vals = universe.asset_flags(equity, None, set(), CHECKS)
    assert "equity_like_weekend_volume" in flags and abs(vals["weekend_volume_ratio"] - 0.1) < 1e-9
    normal, _ = universe.asset_flags(make_candles(30, DAY, MON_2024_01_01, seed=4), None, set(), CHECKS)
    assert normal == []


def test_finalize_statuses():
    cs = [Candidate("AUSDT", "A", median_qvol_30d=9e6, days=30),
          Candidate("BUSDT", "B", median_qvol_30d=8e6, days=30, flags=["wide_spread"]),
          Candidate("CUSDT", "C", median_qvol_30d=4e6, days=30),
          Candidate("DUSDT", "D", median_qvol_30d=9e7, days=12),
          Candidate("EUSDT", "E", median_qvol_30d=7e6, days=30),
          Candidate("FUSDT", "F", status="excluded"),
          Candidate("GUSDT", "G", status="below_prefilter")]
    universe.finalize(cs, floor=5e6, max_size=1, lookback_days=30)
    assert [c.status for c in cs] == ["included", "review", "below_floor", "short_volume_history", "overflow",
                                      "excluded", "below_prefilter"]
    for c in cs[:5]:
        c.status = ""
    universe.finalize(cs, floor=3e6, max_size=10, lookback_days=30)
    assert cs[2].status == "included"  # floor change in settings takes effect


def test_book_metrics():
    bids = [(99.9, 10), (99.5, 10), (98.0, 100)]
    asks = [(100.1, 10), (100.5, 10), (102.0, 100)]
    m = liquidity.book_metrics(bids, asks, [0.5, 1.0], [1_000, 100_000])
    assert abs(m["spread_pct"] - 0.2) < 1e-9
    assert m["depth_bid_0.5pct_usd"] == 99.9 * 10 + 99.5 * 10
    assert m["impact_buy_1000_pct"] == (100.1 - 100) / 100 * 100
    assert m["impact_buy_100000_pct"] is None  # book too thin: not computable, not estimated
    assert liquidity.liquidity_flags(m, max_spread_pct=0.1, min_depth_1pct_usd=50_000) == ["wide_spread", "thin_book_bid", "thin_book_ask"]


def test_depth_is_marked_lower_bound_when_levels_do_not_reach_band():
    m = liquidity.book_metrics([(99.9, 1)], [(100.1, 1)], [1.0], [])
    assert m["depth_bid_1.0pct_usd"] == 99.9 and m["depth_bid_1.0pct_lower_bound"]
    assert m["depth_ask_1.0pct_lower_bound"]
    m2 = liquidity.book_metrics([(99.9, 1), (98.0, 1)], [(100.1, 1), (102.0, 1)], [1.0], [])
    assert not m2["depth_bid_1.0pct_lower_bound"] and not m2["depth_ask_1.0pct_lower_bound"]
