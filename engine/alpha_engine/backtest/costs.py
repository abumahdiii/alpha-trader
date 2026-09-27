"""Spread and commission for the backtest (phase-4 decisions 2 and 5).

Price basis
-----------
Cached MT5 bars are **bid** prices (documented assumption, confirmed by the raw-data check). The ``spread``
column is in POINTS of the symbol (``SymbolSpec.point``)::

    spread_price = spread_points * point
    ask          = bid + spread_price

A buy fills at the ASK open and exits on the BID (low/high/close of the bars); a sell fills at the BID open
and exits on the ASK (bar price + spread_price of the same bar).

Zero / missing spread
---------------------
1. **Causal fill:** a bar with ``spread <= 0`` (or a frame without a spread column) gets the spread of the
   LAST PREVIOUS bar with a non-zero spread (``filled``).
2. **Fallback for bars with no earlier broker spread:** the broker's cached history may carry spread only
   for its most recent part (e.g. zero on every H1 bar before a date), so there is no previous value to
   fill from. Such bars use the constant ``fallback_points`` (``fallback``) when it is > 0; only with a
   fallback of 0 do they really trade at zero spread (``unfilled``).

   The fallback is a COST-MODEL ASSUMPTION, not price information: ``CostModel.fallback_spread_points``
   ``None`` = auto = ``ceil(median of the non-zero spreads of the symbol's FULL cached H1 history)``;
   ``0`` = explicit zero cost; ``> 0`` = a fixed value chosen by the user. The auto value is estimated
   from the whole observed period (also bars after the one it is applied to) -- deliberately: it is one
   constant cost estimate, the strategy's decisions never read the spread, and every bar's PRICES and the
   causal fill (1) still use only data up to that bar. A history without any non-zero spread gives 0 (and
   the zero-cost label). The resolved value and its source are stored with every run
   (``RunResult.spread_fallback``).

All three kinds are counted per window and flagged on trades (``entry_/exit_spread_filled``,
``entry_/exit_spread_fallback``, ``entry_/exit_spread_zero``).

Worked example (gold, point 0.01): spreads ``[25, 0, 0, 31, 0]`` -> used ``[25, 25, 25, 31, 31]`` points,
filled mask ``[F, T, T, F, T]``; bar 1 with bid open 2000.00 -> ask open 2000.00 + 25 * 0.01 = 2000.25.
Leading zeros ``[0, 0, 12]``: fallback 0 -> ``[0, 0, 12]`` with unfilled mask ``[T, T, F]``; fallback 34 ->
``[34, 34, 12]`` with fallback mask ``[T, T, F]``. Auto fallback of the non-zero spreads ``[30, 31, 40, 34]``:
median 32.5 -> ceil -> 33 points.

Commission: ``commission_per_lot_per_side * volume * 2`` (entry + exit side), default 0 -> e.g.
3.50 per lot per side, 0.02 lots -> 3.50 * 0.02 * 2 = 0.14.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

FallbackSource = Literal["auto_median_observed", "user", "none"]


@dataclass(frozen=True)
class SpreadSeries:
    points: np.ndarray  # int64, spread actually used per bar (after the causal fill / fallback)
    price: np.ndarray  # float64, points * point
    filled: np.ndarray  # bool: the raw spread was zero/missing and a previous non-zero one was used
    unfilled: np.ndarray  # bool: zero/missing, no previous non-zero spread and no fallback: 0 used
    has_data: bool  # False when the frame has no spread column or no non-zero spread at all
    fallback: np.ndarray  # bool: zero/missing, no previous non-zero spread, fallback_points (> 0) used
    fallback_points: int = 0


@dataclass(frozen=True)
class ObservedSpread:
    """Non-zero spreads of a whole history (the base of the auto fallback)."""

    count: int
    first_index: int | None  # first bar with a non-zero spread
    last_index: int | None
    median: float | None


def _raw(raw_points: np.ndarray | None, size: int) -> np.ndarray:
    if raw_points is None:
        return np.zeros(size, dtype=np.int64)
    raw = np.asarray(raw_points, dtype=np.int64)
    if raw.shape != (size,):
        raise ValueError("spread array length does not match the bars")
    return raw


def observed_spread(raw_points: np.ndarray | None) -> ObservedSpread:
    if raw_points is None:
        return ObservedSpread(count=0, first_index=None, last_index=None, median=None)
    raw = np.asarray(raw_points, dtype=np.int64)
    good = np.flatnonzero(raw > 0)
    if not len(good):
        return ObservedSpread(count=0, first_index=None, last_index=None, median=None)
    return ObservedSpread(count=int(len(good)), first_index=int(good[0]), last_index=int(good[-1]),
                          median=float(np.median(raw[good])))


def resolve_fallback_points(raw_points: np.ndarray | None, requested: int | None) -> tuple[int, FallbackSource]:
    """``requested`` (user value, 0 allowed) or auto ``ceil(median of the non-zero spreads)`` (0 without data)."""
    if requested is not None:
        if requested < 0:
            raise ValueError("fallback spread must be >= 0 points")
        return int(requested), "user"
    obs = observed_spread(raw_points)
    if obs.median is None:
        return 0, "none"
    return int(math.ceil(obs.median)), "auto_median_observed"


def causal_spread(raw_points: np.ndarray | None, size: int, point: float, fallback_points: int = 0) -> SpreadSeries:
    """Spread per bar: zero/missing values -> the last previous non-zero value, else ``fallback_points``."""
    if not (point > 0):
        raise ValueError(f"symbol point must be > 0, got {point!r}")
    if fallback_points < 0:
        raise ValueError("fallback spread must be >= 0 points")
    raw = _raw(raw_points, size)
    good = raw > 0
    # index of the last good bar at or before i (-1 = none): running maximum of the good positions
    idx = np.where(good, np.arange(size), -1)
    last_good = np.maximum.accumulate(idx) if size else idx
    no_prev = (~good) & (last_good < 0)
    used = np.where(last_good >= 0, raw[np.maximum(last_good, 0)] if size else raw,
                    int(fallback_points)).astype(np.int64)
    filled = (~good) & (last_good >= 0)
    fallback = no_prev & (fallback_points > 0)
    unfilled = no_prev & (fallback_points == 0)
    return SpreadSeries(points=used, price=used * float(point), filled=filled, unfilled=unfilled,
                        has_data=bool(good.any()), fallback=fallback, fallback_points=int(fallback_points))


def spread_from_frame(h1: pd.DataFrame, point: float, fallback_points: int | None = None) -> SpreadSeries:
    """Spread series of an H1 frame; ``fallback_points=None`` = auto (``resolve_fallback_points``)."""
    raw = h1["spread"].to_numpy() if "spread" in h1.columns else None
    if fallback_points is None:
        fallback_points, _ = resolve_fallback_points(raw, None)
    return causal_spread(raw, len(h1), point, fallback_points)


def commission(volume: float, per_lot_per_side: float) -> float:
    """Round-trip commission for ``volume`` lots."""
    return per_lot_per_side * volume * 2.0


__all__ = [
    "FallbackSource",
    "ObservedSpread",
    "SpreadSeries",
    "causal_spread",
    "commission",
    "observed_spread",
    "resolve_fallback_points",
    "spread_from_frame",
]
