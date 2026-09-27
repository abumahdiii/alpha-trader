"""Rolling linear-regression (standard deviation) channel.

Equivalent of MT5's *Standard Deviation Channel* object (see the stddev-channel system definition, section 2),
but **rolling**: at every closed bar ``t`` the channel is re-fitted on the ``n`` closes that END at ``t``
(``close[t-n+1 .. t]``), so a value at ``t`` never depends on bars after ``t`` (no look-ahead).

Math, for one window ``y_0 .. y_{n-1}`` with ``x = 0 .. n-1``::

    xc_i   = x_i - (n-1)/2                     (centered x)
    Sxx    = sum(xc_i^2) = n(n^2-1)/12
    ybar   = mean(y)
    slope  = sum(xc_i * (y_i - ybar)) / Sxx     (price per bar)
    fitted = ybar + slope * xc_i
    mid    = fitted at x = n-1 = ybar + slope * (n-1)/2      (line value AT bar t)
    sigma  = sqrt(sum((y_i - fitted_i)^2) / (n - ddof))       (ddof=0: population, user decision)
    upper  = mid + k*sigma,   lower = mid - k*sigma

Numerics: every window is centered on its own mean before the products are formed and the residuals are
computed explicitly (not as ``Syy - slope^2*Sxx``), so there is no global cumulative-sum drift and no
catastrophic cancellation at price levels like 2000 over 30k+ bars. Windows come from
``numpy.lib.stride_tricks.sliding_window_view`` (a view, no copy) and are processed in row chunks to cap
temporary memory; the only Python loop is over chunks, never over bars.

Worked example (also a test): closes [1,3,2,5,4], n=5, k=2, ddof=0
    ybar=3, xc=[-2,-1,0,1,2], d=y-ybar=[-2,0,-1,2,1], sum(xc*d)=8, Sxx=10 -> slope=0.8
    fitted=[1.4,2.2,3.0,3.8,4.6], resid=[-0.4,0.8,-1.0,1.2,-0.6], SSR=3.6
    sigma=sqrt(3.6/5)=sqrt(0.72)=0.848528137, mid=4.6, upper=6.297056275, lower=2.902943725
    (ddof=1: sigma=sqrt(3.6/4)=sqrt(0.9)=0.948683298)

NaN handling: a window that contains a NaN close yields NaN in every output column for that bar (NaNs are
never filled). Canonical OHLCV frames represent gaps as missing rows, not NaN rows, so this only matters
for malformed input.
"""

from __future__ import annotations

import math
import operator
from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from alpha_engine.logging_setup import get_logger, is_dev_mode

logger = get_logger(__name__)

CHANNEL_COLUMNS = ("mid", "upper", "lower", "slope", "sigma")
# Max elements per temporary (rows * n) while processing windows; ~16 MB of float64.
_CHUNK_ELEMENTS = 2_000_000


def validate_n(n: Any, *, minimum: int = 3, name: str = "n") -> int:
    """Return ``n`` as an int, rejecting bools, non-integers and values below ``minimum``."""
    if isinstance(n, (bool, np.bool_)):
        raise ValueError(f"{name} must be an integer >= {minimum}, got {n!r}")
    try:
        value = operator.index(n)
    except TypeError:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {n!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value}")
    return value


def _validate_k(k: Any) -> float:
    if isinstance(k, (bool, np.bool_)):
        raise ValueError(f"k must be a finite number > 0, got {k!r}")
    try:
        value = float(k)
    except (TypeError, ValueError):
        raise ValueError(f"k must be a finite number > 0, got {k!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"k must be a finite number > 0, got {k!r}")
    return value


def _validate_ddof(ddof: Any) -> int:
    if isinstance(ddof, (bool, np.bool_)) or ddof not in (0, 1):
        raise ValueError(f"ddof must be 0 (population) or 1 (sample), got {ddof!r}")
    return int(ddof)


def _close_values(close: Any) -> tuple[np.ndarray, pd.Index]:
    """1-D float64 values + the index to use for the output."""
    if isinstance(close, pd.DataFrame):
        if "close" not in close.columns:
            raise ValueError("DataFrame input must have a 'close' column")
        close = close["close"]
    if isinstance(close, pd.Series):
        return close.to_numpy(dtype=np.float64, copy=False), close.index
    values = np.asarray(close, dtype=np.float64)
    if values.ndim != 1:
        raise ValueError(f"close must be 1-D, got shape {values.shape}")
    return values, pd.RangeIndex(len(values))


def rolling_regression_channel(close: Any, n: int = 100, k: float = 2.0, ddof: int = 0) -> pd.DataFrame:
    """Rolling least-squares channel over the ``n`` closes ending at each bar.

    Args:
        close: pandas Series (its index is kept), a DataFrame with a ``close`` column, or a 1-D array.
        n: window length in bars (>= 3).
        k: band width in sigmas (> 0).
        ddof: 0 = population sigma (default, user decision), 1 = sample sigma.

    Returns:
        DataFrame with the input's index and columns ``mid, upper, lower, slope, sigma``.
        Rows ``0 .. n-2`` (not enough history) are NaN. ``slope`` is price per bar of the input series.
    """
    n = validate_n(n)
    k = _validate_k(k)
    ddof = _validate_ddof(ddof)
    values, index = _close_values(close)
    total = len(values)
    out = np.full((total, len(CHANNEL_COLUMNS)), np.nan, dtype=np.float64)

    if total >= n:
        xc = np.arange(n, dtype=np.float64) - (n - 1) / 2.0
        sxx = n * (n * n - 1) / 12.0
        half_span = (n - 1) / 2.0
        windows = sliding_window_view(values, n)  # row r = close[r .. r+n-1], ends at bar r+n-1
        rows = windows.shape[0]
        chunk = max(1, _CHUNK_ELEMENTS // n)
        for start in range(0, rows, chunk):  # loop over chunks of windows, not over bars
            w = windows[start:start + chunk]
            ybar = w.mean(axis=1)
            dev = w - ybar[:, None]
            slope = (dev @ xc) / sxx
            resid = dev - slope[:, None] * xc
            ssr = np.einsum("ij,ij->i", resid, resid)
            sigma = np.sqrt(ssr / (n - ddof))
            mid = ybar + slope * half_span
            block = out[n - 1 + start:n - 1 + start + len(w)]
            block[:, 0] = mid
            block[:, 1] = mid + k * sigma
            block[:, 2] = mid - k * sigma
            block[:, 3] = slope
            block[:, 4] = sigma

    result = pd.DataFrame(out, index=index, columns=list(CHANNEL_COLUMNS))
    if is_dev_mode():
        warmup = min(total, n - 1)
        nan_rows = int(np.isnan(out[:, 0]).sum())
        last = out[-1] if total else None
        logger.debug(
            "rolling_regression_channel: n=%d k=%g ddof=%d rows=%d warmup_nan=%d nan_rows_total=%d last=%s",
            n, k, ddof, total, warmup, nan_rows,
            None if last is None else dict(zip(CHANNEL_COLUMNS, np.round(last, 6).tolist())),
        )
    return result


def line_value_at(mid: Any, slope: Any, bars_ahead: Any) -> Any:
    """Project a channel line: ``mid + slope * bars_ahead`` (bars in the channel's own timeframe).

    Works element-wise on scalars, arrays or Series. Apply to ``upper``/``lower`` the same way (they are
    parallel to ``mid``).
    """
    return mid + slope * bars_ahead


def is_flat(slope: Any, n: int, atr_h4: Any, flat_mult: float = 1.0) -> Any:
    """Flat-channel test from the system definition: ``abs(slope) * n < atr_h4 * flat_mult``.

    ``abs(slope) * n`` is the mid-line's change over the whole channel; ``atr_h4`` is Wilder ATR(14) on H4 at
    the same (closed) H4 bar. A flat channel allows trades in both directions.

    NaN policy: if ``slope`` or ``atr_h4`` is NaN the result is **False** (no channel / no ATR yet means we
    cannot call it flat; the caller has no channel to trade anyway). Returns a Python bool for scalar
    inputs, a bool Series (slope's index) when ``slope`` is a Series, otherwise a bool ndarray.
    """
    n = validate_n(n)
    if isinstance(flat_mult, (bool, np.bool_)):
        raise ValueError(f"flat_mult must be a finite number >= 0, got {flat_mult!r}")
    try:
        mult = float(flat_mult)
    except (TypeError, ValueError):
        raise ValueError(f"flat_mult must be a finite number >= 0, got {flat_mult!r}") from None
    if not math.isfinite(mult) or mult < 0:
        raise ValueError(f"flat_mult must be a finite number >= 0, got {flat_mult!r}")

    s = np.asarray(slope, dtype=np.float64)
    a = np.asarray(atr_h4, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        flat = (np.abs(s) * n) < (a * mult)  # any NaN operand -> False
    if isinstance(slope, pd.Series):
        return pd.Series(np.broadcast_to(flat, (len(slope),)).copy(), index=slope.index, name="is_flat")
    if flat.ndim == 0:
        return bool(flat)
    return flat
