"""True Range and Average True Range (Wilder, plus an SMA variant for comparison with MT5).

Definitions (bars in chronological order, gaps are NOT filled)::

    TR_0 = high_0 - low_0
    TR_i = max(high_i - low_i, |high_i - close_{i-1}|, |low_i - close_{i-1}|)     for i >= 1

``close_{i-1}`` is the previous *available* bar: across a weekend or any missing bar the TR uses the last
close that exists (the gap itself is part of the range, as in every standard ATR). Nothing is forward-filled.

Wilder ATR (``period`` = p)::

    ATR_i undefined (NaN)             for i < p-1
    ATR_{p-1} = mean(TR_0 .. TR_{p-1})                      (seed = simple mean of the first p TRs)
    ATR_i     = ((p-1) * ATR_{i-1} + TR_i) / p              for i >= p

The recursion is an exponential average with alpha = 1/p seeded at bar p-1, evaluated with pandas'
compiled ``ewm(adjust=False)`` (no Python per-bar loop); tests check it against the literal formula.

**MT5 difference (tell the user):** MT5's built-in *Average True Range* indicator is a *simple* moving
average of TR (and it skips bar 0 during warm-up), so terminal ATR values will NOT match :func:`wilder_atr`
exactly. :func:`sma_atr` is provided for comparison: from bar ``p`` onward it equals the MT5 indicator
(``max(high, prev_close) - min(low, prev_close)`` is the same TR), only the first value differs because
we include ``TR_0 = high_0 - low_0`` in the first window.

Inputs must be finite (no NaN/inf): the Wilder recursion would otherwise carry a NaN forever.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from alpha_engine.logging_setup import get_logger, is_dev_mode

from .regression_channel import validate_n

logger = get_logger(__name__)


def _hlc(high: Any, low: Any, close: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, pd.Index]:
    arrays = []
    for name, series in (("high", high), ("low", low), ("close", close)):
        values = np.asarray(series.to_numpy() if isinstance(series, pd.Series) else series, dtype=np.float64)
        if values.ndim != 1:
            raise ValueError(f"{name} must be 1-D, got shape {values.shape}")
        if not np.all(np.isfinite(values)):
            raise ValueError(f"{name} contains NaN/inf; ATR inputs must be finite (gaps are missing rows)")
        arrays.append(values)
    h, lo, c = arrays
    if not (len(h) == len(lo) == len(c)):
        raise ValueError(f"high/low/close lengths differ: {len(h)}, {len(lo)}, {len(c)}")
    index = high.index if isinstance(high, pd.Series) else pd.RangeIndex(len(h))
    return h, lo, c, index


def _tr_values(h: np.ndarray, lo: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = h - lo
    if len(tr) > 1:
        prev_close = c[:-1]
        tr[1:] = np.maximum(tr[1:], np.maximum(np.abs(h[1:] - prev_close), np.abs(lo[1:] - prev_close)))
    return tr


def true_range(high: Any, low: Any, close: Any) -> pd.Series:
    """True Range per bar (``TR_0 = high_0 - low_0``). Index follows ``high`` when it is a Series."""
    h, lo, c, index = _hlc(high, low, close)
    return pd.Series(_tr_values(h, lo, c), index=index, name="tr")


def wilder_atr(high: Any, low: Any, close: Any, period: int = 14) -> pd.Series:
    """Wilder's ATR (seed = mean of the first ``period`` TRs, then Wilder smoothing). NaN during warm-up."""
    period = validate_n(period, minimum=1, name="period")
    h, lo, c, index = _hlc(high, low, close)
    tr = _tr_values(h, lo, c)
    atr = np.full(len(tr), np.nan, dtype=np.float64)
    if len(tr) >= period:
        seed = tr[:period].mean()
        seq = np.concatenate(([seed], tr[period:]))
        atr[period - 1:] = pd.Series(seq).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()
    if is_dev_mode():
        logger.debug(
            "wilder_atr: period=%d rows=%d warmup_nan=%d last=%s",
            period, len(tr), min(len(tr), period - 1), None if not len(tr) else float(atr[-1]),
        )
    return pd.Series(atr, index=index, name=f"atr_wilder_{period}")


def sma_atr(high: Any, low: Any, close: Any, period: int = 14) -> pd.Series:
    """Simple-moving-average ATR (what MT5's ATR indicator shows from bar ``period`` onward)."""
    period = validate_n(period, minimum=1, name="period")
    h, lo, c, index = _hlc(high, low, close)
    tr = _tr_values(h, lo, c)
    atr = np.full(len(tr), np.nan, dtype=np.float64)
    if len(tr) >= period:
        atr[period - 1:] = sliding_window_view(tr, period).mean(axis=1)  # exact per window, no drift
    if is_dev_mode():
        logger.debug(
            "sma_atr: period=%d rows=%d warmup_nan=%d last=%s",
            period, len(tr), min(len(tr), period - 1), None if not len(tr) else float(atr[-1]),
        )
    return pd.Series(atr, index=index, name=f"atr_sma_{period}")
