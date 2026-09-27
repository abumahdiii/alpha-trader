"""True Range, Wilder ATR and SMA ATR: hand-computed example, reference loop, gaps, validation."""

from __future__ import annotations

import io
import logging

import numpy as np
import pandas as pd
import pytest

from alpha_engine.indicators.atr import sma_atr, true_range, wilder_atr
from alpha_engine.logging_setup import configure_logging, get_logger

# Hand example (period = 3). Bar 3 gaps up (e.g. Monday open after a weekend).
HIGH = [10.0, 12.0, 11.5, 14.0, 13.0]
LOW = [8.0, 9.5, 10.0, 13.0, 11.0]
CLOSE = [9.0, 11.0, 10.5, 13.5, 11.5]


def test_true_range_hand_example() -> None:
    """TR_0 = 10-8 = 2
    TR_1 = max(12-9.5=2.5, |12-9|=3,    |9.5-9|=0.5)  = 3
    TR_2 = max(11.5-10=1.5, |11.5-11|=0.5, |10-11|=1) = 1.5
    TR_3 = max(14-13=1,    |14-10.5|=3.5, |13-10.5|=2.5) = 3.5   (gap: uses previous available close)
    TR_4 = max(13-11=2,    |13-13.5|=0.5, |11-13.5|=2.5) = 2.5
    """
    tr = true_range(HIGH, LOW, CLOSE)
    assert tr.tolist() == pytest.approx([2.0, 3.0, 1.5, 3.5, 2.5], abs=1e-15)


def test_wilder_atr_hand_example() -> None:
    """period=3:
    ATR_0, ATR_1 = NaN (warm-up)
    ATR_2 = (2 + 3 + 1.5)/3             = 6.5/3  = 13/6   = 2.1666666667   (seed)
    ATR_3 = (2*13/6 + 3.5)/3 = (26/6 + 21/6)/3   = 47/18  = 2.6111111111
    ATR_4 = (2*47/18 + 2.5)/3 = (94/18 + 45/18)/3 = 139/54 = 2.5740740741
    """
    atr = wilder_atr(HIGH, LOW, CLOSE, period=3)
    assert np.isnan(atr.iloc[0]) and np.isnan(atr.iloc[1])
    assert atr.iloc[2] == pytest.approx(13 / 6, rel=1e-14)
    assert atr.iloc[3] == pytest.approx(47 / 18, rel=1e-14)
    assert atr.iloc[4] == pytest.approx(139 / 54, rel=1e-14)
    assert atr.iloc[4] == pytest.approx(2.5740740741, abs=1e-10)


def test_sma_atr_hand_example() -> None:
    """SMA(3) of TR: bar2 = (2+3+1.5)/3 = 13/6; bar3 = (3+1.5+3.5)/3 = 8/3; bar4 = (1.5+3.5+2.5)/3 = 2.5."""
    atr = sma_atr(HIGH, LOW, CLOSE, period=3)
    assert atr.iloc[:2].isna().all()
    assert atr.iloc[2:].tolist() == pytest.approx([13 / 6, 8 / 3, 2.5], rel=1e-14)


def _reference_wilder(tr: np.ndarray, p: int) -> np.ndarray:
    out = np.full(len(tr), np.nan)
    if len(tr) < p:
        return out
    out[p - 1] = sum(tr[:p]) / p
    for i in range(p, len(tr)):
        out[i] = ((p - 1) * out[i - 1] + tr[i]) / p
    return out


def _random_ohlc(size: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    close = 2000.0 + np.cumsum(rng.normal(0, 2.0, size))
    open_ = np.concatenate(([2000.0], close[:-1])) + rng.normal(0, 0.5, size)
    high = np.maximum(open_, close) + rng.exponential(1.0, size)
    low = np.minimum(open_, close) - rng.exponential(1.0, size)
    return high, low, close


@pytest.mark.parametrize("period", [1, 3, 14])
def test_wilder_matches_literal_recursion_on_random_data(period: int) -> None:
    high, low, close = _random_ohlc(30_000, seed=period)
    tr = true_range(high, low, close).to_numpy()
    ref = _reference_wilder(tr, period)
    got = wilder_atr(high, low, close, period=period).to_numpy()
    assert np.array_equal(np.isnan(got), np.isnan(ref))
    np.testing.assert_allclose(got[period - 1:], ref[period - 1:], rtol=1e-12, atol=0)
    assert np.isnan(got[:period - 1]).all()


def test_sma_matches_mt5_style_incremental_sma_after_warmup() -> None:
    """MT5 ATR.mq5: TR_i = max(high, prev_close) - min(low, prev_close); ATR = SMA(TR) from bar `period`."""
    high, low, close = _random_ohlc(2_000, seed=99)
    p = 14
    mt5_tr = np.maximum(high[1:], close[:-1]) - np.minimum(low[1:], close[:-1])  # bars 1..N-1
    mt5_atr = np.array([mt5_tr[i - p:i].mean() for i in range(p, len(high))])  # bars p..N-1
    got = sma_atr(high, low, close, period=p).to_numpy()
    np.testing.assert_allclose(got[p:], mt5_atr, rtol=1e-12, atol=0)
    # ...and Wilder does not (documented difference)
    wil = wilder_atr(high, low, close, period=p).to_numpy()
    assert not np.allclose(wil[p:], mt5_atr, rtol=1e-6)


def test_weekend_gap_is_not_filled() -> None:
    idx = pd.DatetimeIndex(["2024-01-05 16:00", "2024-01-05 20:00", "2024-01-08 00:00"], tz="UTC")
    high = pd.Series([2050.0, 2052.0, 2071.0], index=idx)
    low = pd.Series([2045.0, 2049.0, 2066.0], index=idx)
    close = pd.Series([2048.0, 2051.0, 2068.0], index=idx)
    tr = true_range(high, low, close)
    assert tr.index.equals(idx)  # no bars inserted for the weekend
    # Fri 20:00: max(3, |2052-2048|=4, |2049-2048|=1) = 4
    # Monday: max(5, |2071-2051|=20, |2066-2051|=15) = 20 -> the weekend gap counts once, from Friday's close
    assert tr.tolist() == pytest.approx([5.0, 4.0, 20.0])
    atr = wilder_atr(high, low, close, period=2)
    assert atr.index.equals(idx)
    # seed (5+4)/2 = 4.5 ; then (1*4.5 + 20)/2 = 12.25
    assert atr.iloc[1] == pytest.approx(4.5) and atr.iloc[2] == pytest.approx(12.25)


def test_short_input_all_nan_and_names() -> None:
    atr = wilder_atr(HIGH[:2], LOW[:2], CLOSE[:2], period=3)
    assert len(atr) == 2 and atr.isna().all()
    assert wilder_atr(HIGH, LOW, CLOSE, period=3).name == "atr_wilder_3"
    assert sma_atr(HIGH, LOW, CLOSE, period=3).name == "atr_sma_3"
    assert len(wilder_atr([], [], [], period=14)) == 0


@pytest.mark.parametrize("period", [0, -1, 2.5, True, "14"])
def test_period_validation(period) -> None:
    with pytest.raises(ValueError):
        wilder_atr(HIGH, LOW, CLOSE, period=period)
    with pytest.raises(ValueError):
        sma_atr(HIGH, LOW, CLOSE, period=period)


def test_input_validation() -> None:
    with pytest.raises(ValueError, match="NaN"):
        wilder_atr([1.0, np.nan, 2.0], [0.5, 0.5, 1.0], [0.8, 0.9, 1.5], period=2)
    with pytest.raises(ValueError, match="lengths"):
        true_range([1.0, 2.0], [0.5], [0.8, 1.0])
    with pytest.raises(ValueError):
        true_range(np.ones((2, 2)), np.ones((2, 2)), np.ones((2, 2)))


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev_mode", [True, False])
def test_guarded_summary_log(make_settings, dev_mode: bool) -> None:
    configure_logging(make_settings(f"DEV_MODE={'true' if dev_mode else 'false'}\n"),
                      stream=io.StringIO(), force=True)
    logger = get_logger("alpha_engine.indicators.atr")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        high, low, close = _random_ohlc(300, seed=1)
        wilder_atr(high, low, close)
    finally:
        logger.removeHandler(handler)
    if dev_mode:
        assert len(handler.messages) == 1 and "period=14 rows=300 warmup_nan=13" in handler.messages[0]
    else:
        assert handler.messages == []
