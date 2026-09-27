"""H4 -> H1 alignment and channel projection without look-ahead.

Alignment rule (phase 2 decision log):

* An H1 bar with open time ``T`` is decided at its close, ``D = T + h1_duration``.
* An H4 bar ``j`` is usable only if it is closed by then: ``open_j + h4_duration <= D``.
* ``j* = searchsorted(h4_open + h4_duration, h1_open + h1_duration, side="right") - 1`` (``-1`` = none).
* Times are never floored to 4h boundaries: with DST the broker's H4 boundaries move in UTC
  (e.g. 00/04/08 UTC in winter, 23/03/07 UTC in summer), so only the real bar open times are used.

A formally "closed" H4 bar is one whose nominal end ``open + 4h`` has passed, even if the market stopped
trading inside it (Friday close): it is simply used a little later, which is conservative.

Projection: the channel of H4 bar ``j*`` has its mid-line value ``mid_j`` at ``x = n-1`` (that bar).
For the H1 bar ``T``::

    bar_count mode (default, like MT5's chart axis, which counts bars and skips gaps):
        m          = number of H1 bars with open in [open_{j*}, T)      (actual bars, gaps excluded)
        bars_ahead = m / (h4_duration / h1_duration)                      (= m/4 for H4/H1)
    calendar mode:
        bars_ahead = (T - open_{j*}) / h4_duration                        (elapsed calendar time)

    mid   = mid_j   + slope_j * bars_ahead        (x = (n-1) + bars_ahead)
    upper = upper_j + slope_j * bars_ahead        (= mid +/- k*sigma_j: the band width is unchanged)
    lower = lower_j + slope_j * bars_ahead

Worked example: H4 opens 04:00, 08:00; H1 opens 08:00..11:00 (UTC).
    H1 08:00 (decided 09:00): H4 04:00 closes 08:00 <= 09:00 -> j*=04:00, m=4 (04,05,06,07) -> 1.00
    H1 09:00 -> j*=04:00, m=5 -> 1.25 ;  H1 10:00 -> j*=04:00, m=6 -> 1.50
    H1 11:00 (decided 12:00): H4 08:00 closes 12:00 <= 12:00 -> j*=08:00, m=3 (08,09,10) -> 0.75
In normal data ``bars_ahead`` in bar_count mode lies in [0.75, 1.5]; larger values mean H4 data is missing
or stale, and ``max_bars_ahead`` can blank those rows.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from alpha_engine.logging_setup import get_logger, is_dev_mode

from .regression_channel import CHANNEL_COLUMNS, is_flat, validate_n

logger = get_logger(__name__)

H4 = pd.Timedelta(hours=4)
H1 = pd.Timedelta(hours=1)
PROJECTION_MODES = ("bar_count", "calendar")
# Normal upper bound of bars_ahead in bar_count mode (m <= 6 H1 bars / 4); above it H4 data lags.
_NORMAL_MAX_BARS_AHEAD = 1.5


def _to_utc_ns(times: Any, name: str) -> np.ndarray:
    """tz-aware timestamps -> strictly increasing int64 nanoseconds since epoch (UTC)."""
    if isinstance(times, pd.DataFrame):
        raise ValueError(f"{name}: pass the time column/index, not a DataFrame")
    try:
        idx = pd.DatetimeIndex(times)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: cannot interpret as timestamps ({exc})") from None
    if idx.tz is None:
        raise ValueError(f"{name}: timestamps must be tz-aware (UTC); naive times are ambiguous")
    if idx.hasnans:
        raise ValueError(f"{name}: contains NaT")
    values = idx.tz_convert("UTC").as_unit("ns").asi8
    if len(values) > 1 and not bool(np.all(np.diff(values) > 0)):
        raise ValueError(f"{name}: bar open times must be strictly increasing (sorted, no duplicates)")
    return values


def _td_ns(td: Any, name: str) -> int:
    value = pd.Timedelta(td)
    if pd.isna(value) or value <= pd.Timedelta(0):
        raise ValueError(f"{name} must be a positive duration, got {td!r}")
    return int(value.as_unit("ns").value)


def last_closed_index(
    h4_open_times: Any,
    h1_open_times: Any,
    h4_duration: Any = H4,
    h1_duration: Any = H1,
) -> np.ndarray:
    """Index of the latest H4 bar closed at each H1 bar's close (``-1`` if none). int64 array."""
    h4 = _to_utc_ns(h4_open_times, "h4_open_times")
    h1 = _to_utc_ns(h1_open_times, "h1_open_times")
    d4 = _td_ns(h4_duration, "h4_duration")
    d1 = _td_ns(h1_duration, "h1_duration")
    return (np.searchsorted(h4 + d4, h1 + d1, side="right") - 1).astype(np.int64)


def _bars_since_ns(h4: np.ndarray, j_idx: np.ndarray, h1: np.ndarray) -> np.ndarray:
    m = np.full(len(h1), -1, dtype=np.int64)
    valid = j_idx >= 0
    if not len(h1) or not valid.any():
        return m
    j_open = h4[np.where(valid, j_idx, 0)]
    first = np.searchsorted(h1, j_open, side="left")  # first H1 bar with open >= open_j
    counts = np.arange(len(h1), dtype=np.int64) - first
    # H1 history starting after the H4 bar opened -> the count would be truncated: unknown, not guessed.
    covered = ~((first == 0) & (h1[0] > j_open))
    ok = valid & covered & (counts >= 0)
    m[ok] = counts[ok]
    return m


def h1_bars_since(h4_open_times: Any, j_idx: Any, h1_open_times: Any) -> np.ndarray:
    """``m`` = number of actual H1 bars with open in ``[open_{j*}, T)`` for each H1 bar (``-1`` = unknown).

    Unknown when ``j* == -1`` or when the H1 history starts after ``open_{j*}`` (count would be truncated).
    """
    h4 = _to_utc_ns(h4_open_times, "h4_open_times")
    h1 = _to_utc_ns(h1_open_times, "h1_open_times")
    j = np.asarray(j_idx, dtype=np.int64)
    if j.shape != (len(h1),):
        raise ValueError(f"j_idx must have one entry per H1 bar ({len(h1)}), got shape {j.shape}")
    if len(j) and int(j.max()) >= len(h4):
        raise ValueError("j_idx refers past the end of h4_open_times")
    return _bars_since_ns(h4, j, h1)


def _h1_times(h1_df: Any) -> Any:
    if isinstance(h1_df, pd.DataFrame):
        if "time" in h1_df.columns:
            return h1_df["time"]
        if isinstance(h1_df.index, pd.DatetimeIndex):
            return h1_df.index
        raise ValueError("h1_df needs a 'time' column (bar open time, UTC) or a DatetimeIndex")
    raise ValueError("h1_df must be a DataFrame")


def project_channel_to_h1(
    h4_channel_df: pd.DataFrame,
    h4_open_times: Any,
    h1_df: pd.DataFrame,
    n: int,
    mode: str = "bar_count",
    *,
    h4_atr: Any = None,
    flat_mult: float = 1.0,
    h4_duration: Any = H4,
    h1_duration: Any = H1,
    max_bars_ahead: float | None = None,
) -> pd.DataFrame:
    """Value of the last closed H4 channel at every H1 bar (see module docstring for the rule).

    Args:
        h4_channel_df: output of ``rolling_regression_channel`` on H4 closes, row-aligned (positionally)
            with ``h4_open_times``.
        h4_open_times: H4 bar open times (tz-aware), strictly increasing.
        h1_df: H1 frame; uses its ``time`` column (or DatetimeIndex). Output is indexed like it.
        n: channel length used for ``h4_channel_df`` (needed for the flat test).
        mode: ``"bar_count"`` (default) or ``"calendar"``.
        h4_atr: optional H4 ATR, row-aligned with ``h4_open_times``; adds ``atr_h4`` (value at ``j*``) and
            ``is_flat`` (``abs(slope_j)*n < atr_h4*flat_mult``, NaN -> False).
        max_bars_ahead: optional cap; rows projecting further are blanked (NaN) instead of extrapolated.

    Returns:
        DataFrame with ``mid, upper, lower, slope, sigma, h4_index, bars_ahead`` (+ ``atr_h4, is_flat``).
        ``slope`` is price per H4 bar. Rows without a usable closed H4 channel are NaN (``h4_index`` -1
        when no H4 bar is closed yet).
    """
    n = validate_n(n)
    if mode not in PROJECTION_MODES:
        raise ValueError(f"mode must be one of {PROJECTION_MODES}, got {mode!r}")
    if max_bars_ahead is not None and (not math.isfinite(float(max_bars_ahead)) or max_bars_ahead < 0):
        raise ValueError(f"max_bars_ahead must be a finite number >= 0 or None, got {max_bars_ahead!r}")
    missing = [c for c in CHANNEL_COLUMNS if c not in h4_channel_df.columns]
    if missing:
        raise ValueError(f"h4_channel_df is missing columns {missing}")

    h4 = _to_utc_ns(h4_open_times, "h4_open_times")
    h1 = _to_utc_ns(_h1_times(h1_df), "h1_df time")
    d4 = _td_ns(h4_duration, "h4_duration")
    d1 = _td_ns(h1_duration, "h1_duration")
    if d4 < d1:
        raise ValueError("h4_duration must be >= h1_duration")
    if len(h4_channel_df) != len(h4):
        raise ValueError(f"h4_channel_df has {len(h4_channel_df)} rows but h4_open_times has {len(h4)}")

    j = (np.searchsorted(h4 + d4, h1 + d1, side="right") - 1).astype(np.int64)
    has_j = j >= 0
    jc = np.where(has_j, j, 0)

    if mode == "bar_count":
        m = _bars_since_ns(h4, j, h1)
        bars_ahead = np.where(m >= 0, m / (d4 / d1), np.nan)
    else:
        elapsed = h1 - (h4[jc] if len(h4) else np.zeros(len(h1), dtype=np.int64))
        bars_ahead = np.where(has_j, elapsed / d4, np.nan)
    if max_bars_ahead is not None:
        with np.errstate(invalid="ignore"):
            bars_ahead = np.where(bars_ahead <= max_bars_ahead, bars_ahead, np.nan)

    def pick(values: Any) -> np.ndarray:
        arr = np.asarray(values, dtype=np.float64)
        if not len(h4):
            return np.full(len(h1), np.nan)
        return np.where(has_j, arr[jc], np.nan)

    slope_j = pick(h4_channel_df["slope"].to_numpy())
    shift = slope_j * bars_ahead  # NaN wherever bars_ahead is NaN
    usable = ~np.isnan(bars_ahead)
    out = pd.DataFrame(index=h1_df.index)
    out["mid"] = pick(h4_channel_df["mid"].to_numpy()) + shift
    out["upper"] = pick(h4_channel_df["upper"].to_numpy()) + shift
    out["lower"] = pick(h4_channel_df["lower"].to_numpy()) + shift
    out["slope"] = np.where(usable, slope_j, np.nan)
    out["sigma"] = np.where(usable, pick(h4_channel_df["sigma"].to_numpy()), np.nan)
    out["h4_index"] = j
    out["bars_ahead"] = bars_ahead

    if h4_atr is not None:
        atr_values = np.asarray(h4_atr.to_numpy() if isinstance(h4_atr, pd.Series) else h4_atr, dtype=np.float64)
        if atr_values.shape != (len(h4),):
            raise ValueError(f"h4_atr must have one value per H4 bar ({len(h4)}), got shape {atr_values.shape}")
        atr_j = np.where(usable, pick(atr_values), np.nan)
        out["atr_h4"] = atr_j
        out["is_flat"] = is_flat(out["slope"].to_numpy(), n, atr_j, flat_mult)

    if is_dev_mode():
        with np.errstate(invalid="ignore"):
            stale = int(np.sum(bars_ahead > _NORMAL_MAX_BARS_AHEAD))
        logger.debug(
            "project_channel_to_h1: mode=%s n=%d h4_rows=%d h1_rows=%d no_closed_h4=%d nan_rows=%d "
            "bars_ahead[min=%s max=%s] stale_rows(>%.2f)=%d",
            mode, n, len(h4), len(h1), int((~has_j).sum()), int(np.isnan(out["mid"].to_numpy()).sum()),
            None if not usable.any() else float(np.nanmin(bars_ahead)),
            None if not usable.any() else float(np.nanmax(bars_ahead)),
            _NORMAL_MAX_BARS_AHEAD, stale,
        )
    return out
