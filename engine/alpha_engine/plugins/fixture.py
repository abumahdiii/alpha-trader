"""Deterministic synthetic H1/H4 bars for plugin validation (runtime) and the tests (no MT5, no ``data/``).

:func:`random_walk` is the generator the engine tests have always used (moved here from
``tests/fixtures/channel_scenarios.py``, which re-exports it unchanged, so the golden files keep their numbers):
an FX-like H1 random walk around 2000 with weekends skipped, plus its H4 aggregation on the 00/04/.../20 UTC
grid. Same ``(n_h1, seed, start)`` -> bit-identical frames (``numpy.random.default_rng(seed)``).

:func:`aggregate_h4` rebuilds the H4 frame of any H1 frame (used after :func:`mutate_after`), and
:func:`mutate_after` replaces every H1 bar from index ``cutoff`` on with a different random continuation while
keeping bars ``< cutoff`` untouched -- the "future mutation" of the anti look-ahead check: a causal strategy must
return exactly the same candidates on the bars before the cutoff.

Worked example: ``random_walk(4, seed=7)`` starts Monday 2025-01-06 00:00 UTC with 4 H1 bars 00:00..03:00, whose
single H4 bar opens 00:00 with open = first H1 open, high = max of the highs, low = min of the lows, close = the
03:00 close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLC = ("open", "high", "low", "close")


def _walk(n: int, rng: np.random.Generator, start_close: float) -> tuple[np.ndarray, ...]:
    steps = rng.normal(0.0, 1.2, n)
    close = start_close + np.cumsum(steps)
    open_ = np.r_[start_close, close[:-1]] + rng.normal(0.0, 0.05, n)
    high = np.maximum(open_, close) + rng.exponential(0.7, n)
    low = np.minimum(open_, close) - rng.exponential(0.7, n)
    return open_, high, low, close


def aggregate_h4(h1: pd.DataFrame) -> pd.DataFrame:
    """H4 bars (open on the 00/04/.../20 UTC grid) aggregated from an H1 frame with a ``time`` column."""
    key = h1["time"].dt.floor("4h")
    h4 = (
        h1.groupby(key)
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .reset_index()
    )
    h4.columns = ["time", "open", "high", "low", "close"]
    return h4


def random_walk(n_h1: int, seed: int = 7, start: str = "2025-01-06 00:00") -> tuple[pd.DataFrame, pd.DataFrame]:
    """FX-like H1 random walk (weekends skipped) and its H4 aggregation (bars 00/04/.../20 UTC)."""
    rng = np.random.default_rng(seed)
    hours = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=int(n_h1 * 1.5) + 200, freq="h")
    hours = hours[hours.dayofweek < 5][:n_h1]
    steps = rng.normal(0.0, 1.2, n_h1)
    close = 2000.0 + np.cumsum(steps)
    open_ = np.r_[2000.0, close[:-1]] + rng.normal(0.0, 0.05, n_h1)
    high = np.maximum(open_, close) + rng.exponential(0.7, n_h1)
    low = np.minimum(open_, close) - rng.exponential(0.7, n_h1)
    h1 = pd.DataFrame({"time": hours, "open": open_, "high": high, "low": low, "close": close})
    return h1, aggregate_h4(h1)


def mutate_after(h1: pd.DataFrame, cutoff: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Copy of ``h1`` whose bars ``>= cutoff`` are a new random continuation (same times), and its H4 frame.

    Bars ``< cutoff`` are bit-identical to ``h1``; H4 bars that close at or before the open of bar ``cutoff``
    therefore stay identical too (they only aggregate earlier H1 bars)."""
    n = len(h1)
    if not 0 < cutoff < n:
        raise ValueError(f"cutoff must be inside the frame (1..{n - 1}), got {cutoff}")
    out = h1.copy()
    rng = np.random.default_rng(seed)
    start_close = float(h1["close"].iloc[cutoff - 1])
    open_, high, low, close = _walk(n - cutoff, rng, start_close)
    for column, values in zip(OHLC, (open_, high, low, close), strict=True):
        out.loc[out.index[cutoff:], column] = values
    return out, aggregate_h4(out)


__all__ = ["aggregate_h4", "mutate_after", "random_walk"]
