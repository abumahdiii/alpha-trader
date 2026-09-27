"""Canonical OHLCV frame used everywhere after the MT5 adapter boundary.

Columns (in this order):

============== ========================== =====================================================
column         dtype                      meaning
============== ========================== =====================================================
``time``       ``datetime64[ns, UTC]``    bar OPEN time, in UTC (never broker server time)
``open``       ``float64``
``high``       ``float64``
``low``        ``float64``
``close``      ``float64``
``tick_volume````int64``                  number of ticks in the bar
``spread``     ``int64``                  spread in points (``symbol.point`` units)
``real_volume````int64``                  exchange volume (0 for most CFDs)
============== ========================== =====================================================

Rows are sorted by ``time`` with unique timestamps. Gaps are *kept* as gaps: nothing here (or anywhere
in the engine) forward-fills or synthesises bars (``.claude/rules/03_trading_safety.md`` section 3).

Only closed bars are stored; the adapter drops the bar that is still forming.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

SCHEMA_VERSION = 1
TIME_DTYPE = "datetime64[ns, UTC]"
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
INT_COLUMNS: tuple[str, ...] = ("tick_volume", "spread", "real_volume")
COLUMNS: tuple[str, ...] = ("time", *PRICE_COLUMNS, *INT_COLUMNS)

_EPOCH = pd.Timestamp(0, tz="UTC")


class Timeframe(StrEnum):
    """Timeframes supported in phase 1 (value == the name used in files and the API)."""

    H1 = "H1"
    H4 = "H4"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @property
    def delta(self) -> pd.Timedelta:
        return pd.Timedelta(seconds=self.seconds)

    @classmethod
    def parse(cls, value: str | Timeframe) -> Timeframe:
        """Case-insensitive parse; raises ``ValueError`` with the list of valid values."""
        if isinstance(value, Timeframe):
            return value
        text = str(value).strip().upper()
        try:
            return cls(text)
        except ValueError:
            valid = ", ".join(t.value for t in cls)
            raise ValueError(f"unsupported timeframe {value!r}; expected one of: {valid}") from None


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {Timeframe.H1: 3600, Timeframe.H4: 4 * 3600}


class OhlcvValidationError(ValueError):
    """The frame violates the canonical OHLCV contract."""


def empty_frame() -> pd.DataFrame:
    """A zero-row frame with the canonical columns and dtypes."""
    return normalize_frame(pd.DataFrame({c: [] for c in COLUMNS}))


def normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with canonical column order and dtypes (``time`` -> tz-aware UTC, ns).

    Accepts ``time`` as tz-aware datetimes (converted to UTC) or as naive datetimes that are
    *already UTC*. Integer epochs are rejected on purpose: server-time epochs must go through
    :mod:`alpha_engine.data.timezone` first, so an unconverted epoch can never sneak in.
    Sorting/deduplication is NOT done here (see :func:`sort_dedupe`).
    """
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise OhlcvValidationError(f"missing columns: {missing}")
    out = frame.loc[:, list(COLUMNS)].copy()
    time = out["time"]
    if pd.api.types.is_integer_dtype(time) and len(time):
        raise OhlcvValidationError("'time' must be datetimes (UTC), not raw epoch integers")
    time = pd.to_datetime(time, utc=True)
    out["time"] = time.astype(TIME_DTYPE)
    for col in PRICE_COLUMNS:
        out[col] = out[col].astype("float64")
    for col in INT_COLUMNS:
        out[col] = out[col].astype("int64")
    return out.reset_index(drop=True)


def sort_dedupe(frame: pd.DataFrame, keep: str = "last") -> pd.DataFrame:
    """Sort by time and drop duplicate timestamps (``keep="last"``: later rows win)."""
    out = frame.drop_duplicates(subset="time", keep=keep).sort_values("time", kind="stable")
    return out.reset_index(drop=True)


def validate_frame(frame: pd.DataFrame, timeframe: Timeframe | str | None = None) -> None:
    """Raise :class:`OhlcvValidationError` unless ``frame`` satisfies the canonical contract.

    Checks: exact columns/dtypes, tz-aware UTC ``time``, strictly increasing unique times, no NaN
    prices, ``high >= max(open, close)``, ``low <= min(open, close)``, non-negative volumes/spread and,
    if ``timeframe`` is given, bar times aligned to the timeframe on the *UTC* clock only for H1
    (H4 bars follow the broker's server-time buckets, which in UTC shift with DST -- decision D4).
    """
    if tuple(frame.columns) != COLUMNS:
        raise OhlcvValidationError(f"columns must be {list(COLUMNS)}, got {list(frame.columns)}")
    if str(frame["time"].dtype) != TIME_DTYPE:
        raise OhlcvValidationError(f"'time' dtype must be {TIME_DTYPE}, got {frame['time'].dtype}")
    for col in PRICE_COLUMNS:
        if frame[col].dtype != np.float64:
            raise OhlcvValidationError(f"{col!r} must be float64, got {frame[col].dtype}")
    for col in INT_COLUMNS:
        if frame[col].dtype != np.int64:
            raise OhlcvValidationError(f"{col!r} must be int64, got {frame[col].dtype}")
    if frame.empty:
        return
    times = frame["time"]
    if not times.is_monotonic_increasing:
        raise OhlcvValidationError("'time' is not sorted ascending")
    if times.duplicated().any():
        dup = times[times.duplicated()].iloc[0]
        raise OhlcvValidationError(f"duplicate bar time {dup.isoformat()}")
    prices = frame.loc[:, list(PRICE_COLUMNS)]
    if prices.isna().any().any():
        raise OhlcvValidationError("NaN prices")
    body_high = np.maximum(frame["open"], frame["close"])
    body_low = np.minimum(frame["open"], frame["close"])
    bad_high = frame["high"] < body_high
    if bad_high.any():
        raise OhlcvValidationError(f"high < max(open, close) at {times[bad_high].iloc[0].isoformat()}")
    bad_low = frame["low"] > body_low
    if bad_low.any():
        raise OhlcvValidationError(f"low > min(open, close) at {times[bad_low].iloc[0].isoformat()}")
    for col in INT_COLUMNS:
        if (frame[col] < 0).any():
            raise OhlcvValidationError(f"negative {col}")
    if timeframe is not None and Timeframe.parse(timeframe) is Timeframe.H1:
        misaligned = (to_epoch_seconds(times) % 3600) != 0
        if misaligned.any():
            raise OhlcvValidationError(f"H1 bar not on the hour: {times[misaligned].iloc[0].isoformat()}")


def to_epoch_seconds(times: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """UTC epoch seconds as int64, independent of the datetime unit (s/ms/us/ns)."""
    idx = pd.DatetimeIndex(times)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return np.asarray((idx - _EPOCH) // pd.Timedelta(seconds=1), dtype="int64")


def from_epoch_seconds(seconds: Any) -> pd.Series:
    """Canonical ``time`` series from UTC epoch seconds."""
    arr = np.asarray(seconds, dtype="int64")
    return pd.Series(pd.to_datetime(arr, unit="s", utc=True)).astype(TIME_DTYPE)


def iso_z(ts: pd.Timestamp | None) -> str | None:
    """``2025-01-05T23:00:00Z`` (second precision) or ``None``."""
    if ts is None or pd.isna(ts):
        return None
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
