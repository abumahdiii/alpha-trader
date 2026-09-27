"""Spread and commission for the backtest (phase-4 decisions 2 and 5).

Price basis
-----------
Cached MT5 bars are **bid** prices (documented assumption, confirmed by the raw-data check). The ``spread``
column is in POINTS of the symbol (``SymbolSpec.point``)::

    spread_price = spread_points * point
    ask          = bid + spread_price

A buy fills at the ASK open and exits on the BID (low/high/close of the bars); a sell fills at the BID open
and exits on the ASK (bar price + spread_price of the same bar).

Zero / missing spread (causal fill)
-----------------------------------
A bar with ``spread <= 0`` (or a frame without a spread column) gets the spread of the LAST PREVIOUS bar
with a non-zero spread -- never a later one (no look-ahead). Bars before the first non-zero spread keep 0
("unfilled"). Both kinds are counted and reported with every window.

Worked example (gold, point 0.01): spreads ``[25, 0, 0, 31, 0]`` -> used ``[25, 25, 25, 31, 31]`` points,
filled mask ``[F, T, T, F, T]``; bar 1 with bid open 2000.00 -> ask open 2000.00 + 25 * 0.01 = 2000.25.
Leading ``[0, 0, 12]`` -> ``[0, 0, 12]`` with unfilled mask ``[T, T, F]``.

Commission: ``commission_per_lot_per_side * volume * 2`` (entry + exit side), default 0 -> e.g.
3.50 per lot per side, 0.02 lots -> 3.50 * 0.02 * 2 = 0.14.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class SpreadSeries:
    points: np.ndarray  # int64, spread actually used per bar (after the causal zero fill)
    price: np.ndarray  # float64, points * point
    filled: np.ndarray  # bool: the raw spread was zero/missing and a previous non-zero one was used
    unfilled: np.ndarray  # bool: zero/missing and no previous non-zero spread exists (0 used)
    has_data: bool  # False when the frame has no spread column or no non-zero spread at all


def causal_spread(raw_points: np.ndarray | None, size: int, point: float) -> SpreadSeries:
    """Spread per bar with zero/missing values replaced by the last previous non-zero value."""
    if not (point > 0):
        raise ValueError(f"symbol point must be > 0, got {point!r}")
    if raw_points is None:
        raw = np.zeros(size, dtype=np.int64)
    else:
        raw = np.asarray(raw_points, dtype=np.int64)
        if raw.shape != (size,):
            raise ValueError("spread array length does not match the bars")
    good = raw > 0
    # index of the last good bar at or before i (-1 = none): running maximum of the good positions
    idx = np.where(good, np.arange(size), -1)
    last_good = np.maximum.accumulate(idx) if size else idx
    used = np.where(last_good >= 0, raw[np.maximum(last_good, 0)] if size else raw, 0).astype(np.int64)
    filled = (~good) & (last_good >= 0)
    unfilled = (~good) & (last_good < 0)
    return SpreadSeries(points=used, price=used * float(point), filled=filled, unfilled=unfilled,
                        has_data=bool(good.any()))


def spread_from_frame(h1: pd.DataFrame, point: float) -> SpreadSeries:
    raw = h1["spread"].to_numpy() if "spread" in h1.columns else None
    return causal_spread(raw, len(h1), point)


def commission(volume: float, per_lot_per_side: float) -> float:
    """Round-trip commission for ``volume`` lots."""
    return per_lot_per_side * volume * 2.0


__all__ = ["SpreadSeries", "causal_spread", "commission", "spread_from_frame"]
