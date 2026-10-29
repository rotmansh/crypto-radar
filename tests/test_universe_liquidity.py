import dataclasses

import pytest

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
          Candidate("BUSDT", "B", median_qvol_30d=8e6, days=30, flags=["stable_like_price"]),
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
    assert liquidity.liquidity_warnings(m, max_spread_pct=0.1, min_depth_1pct_usd=50_000) == ["wide_spread", "thin_book_bid", "thin_book_ask"]


def test_depth_is_marked_lower_bound_when_levels_do_not_reach_band():
    m = liquidity.book_metrics([(99.9, 1)], [(100.1, 1)], [1.0], [])
    assert m["depth_bid_1.0pct_usd"] == 99.9 and m["depth_bid_1.0pct_lower_bound"]
    assert m["depth_ask_1.0pct_lower_bound"]
    m2 = liquidity.book_metrics([(99.9, 1), (98.0, 1)], [(100.1, 1), (102.0, 1)], [1.0], [])
    assert not m2["depth_bid_1.0pct_lower_bound"] and not m2["depth_ask_1.0pct_lower_bound"]


LIQ = {"max_spread_pct": 0.3, "min_depth_1pct_usd": 50_000, "suspend_window": 6, "suspend_min_hits": 5,
       "suspend_min_span_hours": 24}


def snap(hour, thin):
    d = 10_000 if thin else 500_000
    return {"measured_utc": f"2026-10-{20 + hour // 24:02d}T{hour % 24:02d}:00:00+00:00", "spread_pct": 0.01,
            "depth_bid_1.0pct_usd": d, "depth_ask_1.0pct_usd": d,
            "depth_bid_1.0pct_lower_bound": False, "depth_ask_1.0pct_lower_bound": False}


def test_single_thin_snapshot_never_suspends():
    assert not liquidity.persistence([snap(0, True)], LIQ)["suspended"]


def test_suspension_needs_window_hits_and_span():
    six_quick = [snap(h, True) for h in range(6)]  # 6/6 thin but only 5 hours apart
    assert not liquidity.persistence(six_quick, LIQ)["suspended"]
    six_slow = [snap(h * 6, True) for h in range(6)]  # 30 hours
    assert liquidity.persistence(six_slow, LIQ)["suspended"]
    mixed = [snap(h * 6, h % 2 == 0) for h in range(6)]  # only 3/6 thin
    assert not liquidity.persistence(mixed, LIQ)["suspended"]


def test_suspension_holds_at_a_4h_cadence():
    # regression (2026-10-29): runs every 4h put 6 measurements only 20h apart, which lifted GTC/MUBARAK at 6/6 thin
    every_4h = [snap(h * 4, True) for h in range(9)]               # 32h of thin books
    p = liquidity.persistence(every_4h, LIQ)
    assert p["suspended"] and p["measurements"] == 7 and p["span_hours"] == 24.0
    assert not liquidity.persistence(every_4h[:6], LIQ)["suspended"]  # only 20h of evidence: not yet
    recovering = every_4h[:5] + [snap(h * 4, False) for h in range(5, 9)]   # 4 of the last 7 healthy
    assert not liquidity.persistence(recovering, LIQ)["suspended"]
    one_good = every_4h[:8] + [snap(32, False)]                      # 6 of 7 thin still >= 5/6
    assert liquidity.persistence(one_good, LIQ)["suspended"]


def test_parse_measurement_round_trip_from_csv_strings():
    row = {"measured_utc": "2026-10-27T06:00:00+00:00", "spread_pct": "0.05", "depth_bid_1.0pct_usd": "",
           "depth_ask_1.0pct_usd": "40000.0", "depth_ask_1.0pct_lower_bound": "False"}
    m = liquidity.parse_measurement(row)
    assert m["depth_bid_1.0pct_lower_bound"] is True and m["depth_ask_1.0pct_usd"] == 40000.0
    assert liquidity.liquidity_warnings(m, 0.3, 50_000) == ["thin_book_ask"]


def test_liquidity_warning_does_not_change_membership():
    c = Candidate("AUSDT", "A", median_qvol_30d=9e6, days=30, liquidity_warnings=["thin_book_ask"], tradable=True)
    universe.finalize([c], floor=5e6, max_size=10, lookback_days=30)
    assert c.status == "included"
    c2 = Candidate("BUSDT", "B", median_qvol_30d=9e6, days=30, liquidity_suspended=True, tradable=True)
    universe.finalize([c2], floor=5e6, max_size=10, lookback_days=30)
    assert c2.status == "liquidity_suspended"


REVIEW = {"verified": {"Z": {"accepted_flags": ["permission_profile_anomaly"], "decided": "d", "evidence": "e"}},
          "hold": {"H": {"decided": "d", "question": "q?"}}}


def test_verified_pair_passes_only_with_accepted_flags_and_tradable():
    ok = Candidate("ZUSDT", "Z", median_qvol_30d=9e6, days=30, flags=["permission_profile_anomaly"], tradable=True)
    extra = Candidate("ZUSDT", "Z", median_qvol_30d=9e6, days=30,
                      flags=["permission_profile_anomaly", "equity_like_weekend_volume"], tradable=True)
    halted = Candidate("ZUSDT", "Z", median_qvol_30d=9e6, days=30, flags=["permission_profile_anomaly"], tradable=False)
    other = Candidate("QUSDT", "Q", median_qvol_30d=9e6, days=30, flags=["permission_profile_anomaly"], tradable=True)
    for c in (ok, extra, halted, other):
        universe.finalize([c], floor=5e6, max_size=10, lookback_days=30, review=REVIEW)
    assert ok.status == "included" and ok.notes
    assert extra.status == "review" and "equity_like_weekend_volume" in extra.review_reason
    assert halted.status == "review" and halted.review_reason == "not_tradable"
    assert other.status == "review"  # verification is per asset, not per flag


def test_hold_keeps_clean_pair_in_review():
    c = Candidate("HUSDT", "H", median_qvol_30d=9e6, days=30, tradable=True)
    universe.finalize([c], floor=5e6, max_size=10, lookback_days=30, review=REVIEW)
    assert c.status == "review" and c.review_reason.startswith("manual_hold")


def test_tradability():
    candles = make_candles(10, DAY, MON_2024_01_01)
    assert universe.tradability(candles, True, 7)[0]
    assert not universe.tradability(candles, False, 7)[0]
    dead = candles[:-1] + [dataclasses.replace(candles[-1], volume=0.0)]
    assert not universe.tradability(dead, True, 7)[0]


def test_review_file_loads():
    r = config.load_review()
    assert set(r["verified"]) == {"ZEC", "DASH"} and not r["hold"]
    # decisions of 2026-10-28: POL back under the normal rules; GTC/MUBARAK/牛来 temporarily suspended
    assert r["suspended"]["GTC"]["lift_when"] == r["suspended"]["MUBARAK"]["lift_when"] == "liquidity_ok"
    assert r["suspended"]["牛来"]["lift_when"] == "asset_verified_and_liquidity_ok" and "POL" not in r["suspended"]


SUSP = {"verified": {}, "hold": {}, "suspended": {
    "L": {"decided": "d", "lift_when": "liquidity_ok", "reason": "thin", "evidence": "e"},
    "N": {"decided": "d", "lift_when": "asset_verified_and_liquidity_ok", "reason": "type?", "evidence": "e"}}}


def test_liquidity_suspension_follows_the_rule_and_lifts_by_itself():
    thin = Candidate("LUSDT", "L", median_qvol_30d=9e6, days=30, tradable=True, liquidity_suspended=True)
    better = Candidate("LUSDT", "L", median_qvol_30d=9e6, days=30, tradable=True, liquidity_suspended=False)
    for c in (thin, better):
        universe.finalize([c], floor=5e6, max_size=10, lookback_days=30, review=SUSP)
    assert thin.status == "liquidity_suspended" and "suspended d" in thin.notes[0]
    assert better.status == "included" and "lifted" in better.notes[0]


def test_suspension_pending_asset_verification_needs_a_verified_decision_first():
    clean = Candidate("NUSDT", "N", median_qvol_30d=9e6, days=30, tradable=True)   # flags gone, still unverified
    universe.finalize([clean], floor=5e6, max_size=10, lookback_days=30, review=SUSP)
    assert clean.status == "review" and clean.review_reason.startswith("suspended_pending_asset_verification")
    verified = {**SUSP, "verified": {"N": {"accepted_flags": ["permission_profile_anomaly"], "decided": "d2", "evidence": "e"}}}
    thin = Candidate("NUSDT", "N", median_qvol_30d=9e6, days=30, tradable=True, flags=["permission_profile_anomaly"],
                     liquidity_suspended=True)
    ok = Candidate("NUSDT", "N", median_qvol_30d=9e6, days=30, tradable=True, flags=["permission_profile_anomaly"])
    for c in (thin, ok):
        universe.finalize([c], floor=5e6, max_size=10, lookback_days=30, review=verified)
    assert thin.status == "liquidity_suspended" and ok.status == "included"


def test_unknown_lift_rule_is_rejected(tmp_path):
    p = tmp_path / "review.toml"
    p.write_text('[suspended.X]\ndecided="d"\nlift_when="never"\nreason="r"\nevidence="e"\n')
    with pytest.raises(config.ConfigError):
        config.load_review(p)
