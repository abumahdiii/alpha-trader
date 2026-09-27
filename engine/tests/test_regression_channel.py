"""Rolling regression channel: worked example, naive-polyfit equivalence, no look-ahead, stability, speed."""

from __future__ import annotations

import io
import logging
import math
import time

import numpy as np
import pandas as pd
import pytest

from alpha_engine.indicators.regression_channel import (
    is_flat,
    line_value_at,
    rolling_regression_channel,
)
from alpha_engine.logging_setup import configure_logging, get_logger


def naive_channel(close: np.ndarray, n: int, k: float, ddof: int) -> np.ndarray:
    """Reference: per-window np.polyfit (slow, obviously correct). Columns mid, upper, lower, slope, sigma."""
    out = np.full((len(close), 5), np.nan)
    x = np.arange(n, dtype=float)
    for t in range(n - 1, len(close)):
        y = close[t - n + 1:t + 1]
        slope, intercept = np.polyfit(x, y, 1)
        fitted = intercept + slope * x
        sigma = math.sqrt(float(np.sum((y - fitted) ** 2)) / (n - ddof))
        mid = fitted[-1]
        out[t] = (mid, mid + k * sigma, mid - k * sigma, slope, sigma)
    return out


def random_walk(size: int, start: float, scale: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start + np.cumsum(rng.normal(0.0, scale, size))


# --- worked example ------------------------------------------------------------------------------

def test_worked_example_population_sigma() -> None:
    """closes [1,3,2,5,4], n=5, k=2, ddof=0.

    ybar=3, xc=[-2,-1,0,1,2], dev=[-2,0,-1,2,1], sum(xc*dev)=8, Sxx=10 -> slope=0.8
    fitted=[1.4,2.2,3.0,3.8,4.6], resid=[-0.4,0.8,-1.0,1.2,-0.6], SSR=0.16+0.64+1+1.44+0.36=3.6
    sigma=sqrt(3.6/5)=sqrt(0.72)=0.848528137..., mid=4.6, upper=4.6+2*0.8485=6.297056275, lower=2.902943725
    """
    ch = rolling_regression_channel(pd.Series([1.0, 3.0, 2.0, 5.0, 4.0]), n=5, k=2.0, ddof=0)
    assert ch.iloc[:4].isna().all().all()
    row = ch.iloc[4]
    assert row["slope"] == pytest.approx(0.8, rel=1e-12)
    assert row["mid"] == pytest.approx(4.6, rel=1e-12)
    assert row["sigma"] == pytest.approx(math.sqrt(0.72), rel=1e-12)
    assert row["sigma"] == pytest.approx(0.848528137, abs=1e-9)
    assert row["upper"] == pytest.approx(6.297056275, abs=1e-9)
    assert row["lower"] == pytest.approx(2.902943725, abs=1e-9)
    # fitted line = mid - slope*(n-1-x): [1.4, 2.2, 3.0, 3.8, 4.6]
    fitted = [line_value_at(row["mid"], row["slope"], x - 4) for x in range(5)]
    assert fitted == pytest.approx([1.4, 2.2, 3.0, 3.8, 4.6], rel=1e-12)
    resid = np.array([1.0, 3.0, 2.0, 5.0, 4.0]) - np.array(fitted)
    assert resid == pytest.approx([-0.4, 0.8, -1.0, 1.2, -0.6], abs=1e-12)


def test_worked_example_sample_sigma() -> None:
    """Same window with ddof=1: sigma = sqrt(3.6/4) = sqrt(0.9) = 0.948683298..."""
    row = rolling_regression_channel([1.0, 3.0, 2.0, 5.0, 4.0], n=5, k=2.0, ddof=1).iloc[4]
    assert row["sigma"] == pytest.approx(math.sqrt(0.9), rel=1e-12)
    assert row["sigma"] == pytest.approx(0.948683298, abs=1e-9)
    assert row["mid"] == pytest.approx(4.6, rel=1e-12)
    assert row["upper"] == pytest.approx(4.6 + 2 * math.sqrt(0.9), rel=1e-12)
    assert row["lower"] == pytest.approx(4.6 - 2 * math.sqrt(0.9), rel=1e-12)


def test_output_shape_index_and_warmup() -> None:
    idx = pd.date_range("2024-01-01", periods=30, freq="4h", tz="UTC")
    close = pd.Series(random_walk(30, 100.0, 1.0, 1), index=idx)
    ch = rolling_regression_channel(close, n=10)
    assert list(ch.columns) == ["mid", "upper", "lower", "slope", "sigma"]
    assert ch.index.equals(idx)
    assert ch.iloc[:9].isna().all().all()
    assert ch.iloc[9:].notna().all().all()
    # DataFrame input with a 'close' column gives the same result
    frame = pd.DataFrame({"time": idx, "close": close.to_numpy()}, index=idx)
    pd.testing.assert_frame_equal(rolling_regression_channel(frame, n=10), ch)


def test_shorter_than_window_is_all_nan() -> None:
    ch = rolling_regression_channel([1.0, 2.0, 3.0], n=5)
    assert len(ch) == 3 and ch.isna().all().all()
    assert len(rolling_regression_channel([], n=5)) == 0


def test_perfect_line_has_zero_sigma() -> None:
    close = 2000.0 + 0.25 * np.arange(50)
    ch = rolling_regression_channel(close, n=20)
    np.testing.assert_allclose(ch["slope"].iloc[19:], 0.25, rtol=1e-12)
    np.testing.assert_allclose(ch["mid"].iloc[19:], close[19:], rtol=1e-14)
    assert np.all(ch["sigma"].iloc[19:] < 1e-9)


def test_nan_in_window_propagates_and_is_not_filled() -> None:
    close = random_walk(40, 50.0, 1.0, 3)
    close[20] = np.nan
    ch = rolling_regression_channel(close, n=5)
    assert ch.iloc[20:25].isna().all().all()  # every window containing bar 20
    assert ch.iloc[[19, 25]].notna().all().all()


# --- equivalence with the naive reference --------------------------------------------------------

@pytest.mark.parametrize("n,ddof", [(100, 0), (100, 1), (20, 0), (3, 0)])
def test_matches_naive_polyfit_on_random_data(n: int, ddof: int) -> None:
    close = random_walk(2500, 1800.0, 3.0, seed=42 + n + ddof)
    fast = rolling_regression_channel(close, n=n, k=2.0, ddof=ddof).to_numpy()
    ref = naive_channel(close, n, 2.0, ddof)
    assert np.array_equal(np.isnan(fast), np.isnan(ref))
    np.testing.assert_allclose(fast[n - 1:], ref[n - 1:], rtol=1e-9, atol=0)


def test_numerically_stable_over_30k_bars_near_2000() -> None:
    close = random_walk(30_000, 2000.0, 1.5, seed=7)
    fast = rolling_regression_channel(close, n=100).to_numpy()
    for t in (99, 100, 5_000, 14_999, 22_222, 29_998, 29_999):
        ref = naive_channel(close[t - 99:t + 1], 100, 2.0, 0)[-1]
        np.testing.assert_allclose(fast[t], ref, rtol=1e-9, atol=0, err_msg=f"bar {t}")


def test_small_moves_at_high_price_level_keep_precision() -> None:
    # price ~2000 with 1e-3 moves: a naive sum-of-squares formula would lose most digits here
    close = 2000.0 + np.cumsum(np.random.default_rng(11).normal(0, 1e-3, 3000))
    fast = rolling_regression_channel(close, n=100).to_numpy()
    for t in (99, 1500, 2999):
        ref = naive_channel(close[t - 99:t + 1], 100, 2.0, 0)[-1]
        np.testing.assert_allclose(fast[t], ref, rtol=1e-7, atol=0, err_msg=f"bar {t}")


# --- no look-ahead -------------------------------------------------------------------------------

def test_values_unchanged_when_future_bars_are_appended_or_altered() -> None:
    close = random_walk(3000, 1900.0, 2.0, seed=5)
    base = rolling_regression_channel(close[:1000], n=100).to_numpy()
    longer = rolling_regression_channel(close, n=100).to_numpy()
    np.testing.assert_array_equal(longer[:1000], base)  # bit-identical, NaN warm-up included
    tampered = close.copy()
    tampered[1000:] = 1e6  # garbage future
    altered = rolling_regression_channel(tampered, n=100).to_numpy()
    np.testing.assert_array_equal(altered[:1000], base)
    assert not np.allclose(altered[1000], longer[1000])


def test_value_at_t_depends_only_on_last_n_closes() -> None:
    close = random_walk(500, 100.0, 1.0, seed=9)
    ch = rolling_regression_channel(close, n=50).to_numpy()
    for t in (49, 250, 499):
        alone = rolling_regression_channel(close[t - 49:t + 1], n=50).to_numpy()[-1]
        np.testing.assert_array_equal(ch[t], alone)


@pytest.mark.parametrize(("n", "ddof"), [(100, 0), (100, 1), (37, 0)])
def test_prefix_and_full_history_are_bit_identical(n: int, ddof: int) -> None:
    """Determinism fix: no BLAS product, so a bar's channel does not depend on how many bars follow it."""
    close = random_walk(3000, 2000.0, 1.7, seed=21)
    full = rolling_regression_channel(close, n=n, k=2.0, ddof=ddof).to_numpy()
    for length in (n, n + 1, 101, 257, 1000, 1999, 2998, 3000):
        prefix = rolling_regression_channel(close[:length], n=n, k=2.0, ddof=ddof).to_numpy()
        # assert_array_equal treats NaN == NaN and compares floats exactly (no tolerance).
        np.testing.assert_array_equal(prefix, full[:length], err_msg=f"prefix length {length}")
        assert prefix.tobytes() == full[:length].tobytes()  # bit for bit, incl. the sign of zero


def test_chunk_boundaries_do_not_change_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows are reduced independently: splitting the windows into many small chunks changes nothing."""
    from alpha_engine.indicators import regression_channel as rc

    close = random_walk(3000, 80.0, 0.3, seed=22)
    reference = rolling_regression_channel(close, n=100).to_numpy()
    monkeypatch.setattr(rc, "_CHUNK_ELEMENTS", 100 * 7)  # 7 windows per chunk
    assert rolling_regression_channel(close, n=100).to_numpy().tobytes() == reference.tobytes()


# --- speed ---------------------------------------------------------------------------------------

def test_30k_bars_under_one_second() -> None:
    close = random_walk(30_000, 2000.0, 1.5, seed=13)
    rolling_regression_channel(close[:500], n=100)  # warm imports / caches
    start = time.perf_counter()
    rolling_regression_channel(close, n=100)
    assert time.perf_counter() - start < 1.0


# --- helpers and validation ----------------------------------------------------------------------

def test_line_value_at_projection() -> None:
    assert line_value_at(4.6, 0.8, 1.25) == pytest.approx(5.6)
    np.testing.assert_allclose(line_value_at(np.array([1.0, 2.0]), np.array([0.5, -1.0]), 2.0), [2.0, 0.0])


def test_is_flat_formula_and_nan_policy() -> None:
    # |slope|*n = 0.05*100 = 5.0 vs atr*mult
    assert is_flat(0.05, 100, 6.0) is True
    assert is_flat(-0.05, 100, 6.0) is True          # sign does not matter
    assert is_flat(0.05, 100, 5.0) is False          # strict "<": equal is not flat
    assert is_flat(0.05, 100, 6.0, flat_mult=0.5) is False  # 5.0 < 3.0 is False
    assert is_flat(0.05, 100, 4.0, flat_mult=1.5) is True   # 5.0 < 6.0
    assert is_flat(float("nan"), 100, 6.0) is False
    assert is_flat(0.05, 100, float("nan")) is False
    arr = is_flat(np.array([0.01, 0.2, np.nan]), 100, np.array([2.0, 2.0, 2.0]))
    assert arr.dtype == bool and arr.tolist() == [True, False, False]
    s = pd.Series([0.01, 0.2], index=[10, 11])
    res = is_flat(s, 100, 2.0)
    assert isinstance(res, pd.Series) and res.index.tolist() == [10, 11] and res.tolist() == [True, False]


@pytest.mark.parametrize("kwargs", [
    {"n": 2}, {"n": 0}, {"n": 3.5}, {"n": True}, {"n": "100"},
    {"k": 0}, {"k": -1.0}, {"k": float("nan")}, {"k": float("inf")},
    {"ddof": 2}, {"ddof": -1}, {"ddof": True}, {"ddof": 0.5},
])
def test_parameter_validation(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        rolling_regression_channel(np.arange(10.0), **kwargs)


def test_numpy_integer_n_accepted_and_2d_rejected() -> None:
    rolling_regression_channel(np.arange(10.0), n=np.int64(5))
    with pytest.raises(ValueError):
        rolling_regression_channel(np.ones((5, 2)), n=3)
    with pytest.raises(ValueError):
        rolling_regression_channel(pd.DataFrame({"open": [1.0, 2.0, 3.0]}), n=3)


@pytest.mark.parametrize("bad", [-0.1, float("nan"), True, "x"])
def test_is_flat_validation(bad) -> None:
    with pytest.raises(ValueError):
        is_flat(0.1, 100, 1.0, flat_mult=bad)
    with pytest.raises(ValueError):
        is_flat(0.1, 2, 1.0)


# --- logging -------------------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev_mode", [True, False])
def test_one_guarded_summary_log_per_call(make_settings, dev_mode: bool) -> None:
    configure_logging(make_settings(f"DEV_MODE={'true' if dev_mode else 'false'}\n"),
                      stream=io.StringIO(), force=True)
    logger = get_logger("alpha_engine.indicators.regression_channel")
    handler = _ListHandler()
    logger.addHandler(handler)
    try:
        rolling_regression_channel(random_walk(500, 100.0, 1.0, 1), n=100)
    finally:
        logger.removeHandler(handler)
    if dev_mode:
        assert len(handler.messages) == 1
        assert "n=100" in handler.messages[0] and "rows=500" in handler.messages[0]
        assert "warmup_nan=99" in handler.messages[0]
    else:
        assert handler.messages == []
