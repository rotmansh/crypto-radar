import math

import numpy as np
import pandas as pd
from conftest import DAY, make_candles

from radar import indicators as ind


def frame(n=400, seed=1):
    return ind.to_frame(make_candles(n, DAY, 1_704_067_200_000, seed=seed))


def test_sma_and_bollinger_hand_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0] and math.isnan(ind.sma(s, 3)[1])
    bb = ind.bollinger(s, 5, 2.0)
    sd = math.sqrt(2.0)  # population σ of 1..5
    assert abs(bb["bb_upper"].iloc[-1] - (3 + 2 * sd)) < 1e-12 and abs(bb["bb_lower"].iloc[-1] - (3 - 2 * sd)) < 1e-12


def test_atr_wilder_matches_reference_loop():
    df = frame(60)
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    ref = [float("nan")] * 13 + [sum(tr[:14]) / 14]
    for i in range(14, len(tr)):
        ref.append((ref[-1] * 13 + tr[i]) / 14)
    got = ind.atr_wilder(df, 14).to_numpy()
    assert np.isnan(got[:13]).all() and np.allclose(got[13:], ref[13:], rtol=0, atol=1e-12)


def test_percentile_rank_and_prev_volume():
    s = pd.Series([5.0, 1, 3, 2, 4])
    pr = ind.percentile_rank(s, 5)
    assert pr.iloc[-1] == 80.0  # 4 of 5 values <= 4
    v = pd.Series([10.0, 20, 30, 1000])
    assert ind.volume_sma_prev(v, 3).iloc[-1] == 20.0  # excludes the current candle


def test_realized_vol():
    close = pd.Series([100.0, 110, 99, 108.9])
    r = np.log(close / close.shift(1)).dropna()
    expected = r.std(ddof=1) * math.sqrt(365)
    assert abs(ind.realized_vol(close, 3).iloc[-1] - expected) < 1e-12


def test_relative_strength_aligns_without_forward_fill():
    coin, btc = frame(10, 1), frame(10, 2)
    btc = btc.drop(btc.index[4])
    rs = ind.relative_strength(coin, btc)
    assert len(rs) == 9 and coin.index[4] not in rs.index
    t = coin.index[5]
    assert rs[t] == coin.loc[t, "close"] / btc.loc[t, "close"]


def test_no_lookahead():
    """Truncating the data after t must not change any indicator value at or before t."""
    df = frame(400, 7)
    full = ind.compute_all(df)
    for t in (199, 230, 280, 340, 399):
        part = ind.compute_all(df.iloc[: t + 1])
        pd.testing.assert_frame_equal(full.iloc[: t + 1], part, check_exact=False, rtol=0, atol=1e-9)


def test_warmup_is_nan_not_guessed():
    out = ind.compute_all(frame(250))
    assert out["sma200"].iloc[:199].isna().all() and not math.isnan(out["sma200"].iloc[199])
    assert out["bb_width_pct"].iloc[:198].isna().all()  # needs 19 bars for BB + 180 for the rank
