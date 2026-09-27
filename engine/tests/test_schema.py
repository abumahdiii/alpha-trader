from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha_engine.data.schema import (
    COLUMNS,
    TIME_DTYPE,
    OhlcvValidationError,
    Timeframe,
    empty_frame,
    from_epoch_seconds,
    iso_z,
    normalize_frame,
    sort_dedupe,
    to_epoch_seconds,
    validate_frame,
)


def _frame(times=("2025-01-06T00:00Z", "2025-01-06T01:00Z"), **over) -> pd.DataFrame:
    n = len(times)
    data = {"time": pd.to_datetime(list(times), utc=True), "open": [10.0] * n, "high": [11.0] * n,
            "low": [9.0] * n, "close": [10.5] * n, "tick_volume": [5] * n, "spread": [2] * n,
            "real_volume": [0] * n}
    data.update(over)
    return normalize_frame(pd.DataFrame(data))


def test_timeframes() -> None:
    assert Timeframe.H1.seconds == 3600 and Timeframe.H4.seconds == 14400
    assert Timeframe.parse("h4") is Timeframe.H4
    with pytest.raises(ValueError, match="H1, H4"):
        Timeframe.parse("M5")


def test_normalize_gives_canonical_dtypes_and_order() -> None:
    frame = _frame()
    assert tuple(frame.columns) == COLUMNS
    assert str(frame["time"].dtype) == TIME_DTYPE
    validate_frame(frame, "H1")
    assert list(empty_frame().columns) == list(COLUMNS)
    validate_frame(empty_frame())


def test_normalize_rejects_raw_epoch_integers() -> None:
    raw = pd.DataFrame({c: [1] for c in COLUMNS})
    with pytest.raises(OhlcvValidationError, match="epoch"):
        normalize_frame(raw)


@pytest.mark.parametrize(("over", "match"), [
    ({"high": [9.5, 11.0]}, "high < max"),
    ({"low": [10.6, 9.0]}, "low > min"),
    ({"open": [np.nan, 10.0]}, "NaN"),
    ({"spread": [-1, 2]}, "negative spread"),
])
def test_validate_catches_bad_bars(over, match) -> None:
    with pytest.raises(OhlcvValidationError, match=match):
        validate_frame(_frame(**over))


def test_validate_catches_order_duplicates_alignment() -> None:
    with pytest.raises(OhlcvValidationError, match="sorted"):
        validate_frame(_frame(times=("2025-01-06T01:00Z", "2025-01-06T00:00Z")))
    with pytest.raises(OhlcvValidationError, match="duplicate"):
        validate_frame(_frame(times=("2025-01-06T01:00Z", "2025-01-06T01:00Z")))
    with pytest.raises(OhlcvValidationError, match="on the hour"):
        validate_frame(_frame(times=("2025-01-06T01:30Z", "2025-01-06T02:30Z")), "H1")
    validate_frame(_frame(times=("2025-01-06T01:00Z", "2025-01-06T05:00Z")), "H4")


def test_sort_dedupe_keeps_last() -> None:
    a = _frame(times=("2025-01-06T01:00Z",))
    b = _frame(times=("2025-01-06T01:00Z", "2025-01-06T00:00Z"), close=[10.9, 10.1])
    merged = sort_dedupe(pd.concat([a, b], ignore_index=True))
    assert merged["close"].tolist() == [10.1, 10.9]


def test_epoch_helpers_unit_independent() -> None:
    for unit in ("s", "ms", "us", "ns"):
        s = pd.Series(pd.to_datetime([1736121600], unit="s", utc=True)).astype(f"datetime64[{unit}, UTC]")
        assert to_epoch_seconds(s).tolist() == [1736121600]
    assert iso_z(from_epoch_seconds([1736121600]).iloc[0]) == "2025-01-06T00:00:00Z"
    assert iso_z(None) is None
