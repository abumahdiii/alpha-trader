"""Vectorized, look-ahead-free indicators for the StdDev channel system (phase 2, group 2B)."""

from .atr import sma_atr, true_range, wilder_atr
from .mtf import h1_bars_since, last_closed_index, project_channel_to_h1
from .regression_channel import is_flat, line_value_at, rolling_regression_channel

__all__ = [
    "h1_bars_since",
    "is_flat",
    "last_closed_index",
    "line_value_at",
    "project_channel_to_h1",
    "rolling_regression_channel",
    "sma_atr",
    "true_range",
    "wilder_atr",
]
