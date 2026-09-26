"""Price-action pattern detectors (pure, vectorized, no look-ahead)."""

from .candles import (
    PATTERN_COLUMNS,
    PATTERNS,
    bearish_engulfing,
    bearish_pin_bar,
    bearish_reversal,
    bullish_engulfing,
    bullish_pin_bar,
    bullish_reversal,
    candle_anatomy,
    detect_reversals,
)

__all__ = [
    "PATTERNS",
    "PATTERN_COLUMNS",
    "bearish_engulfing",
    "bearish_pin_bar",
    "bearish_reversal",
    "bullish_engulfing",
    "bullish_pin_bar",
    "bullish_reversal",
    "candle_anatomy",
    "detect_reversals",
]
