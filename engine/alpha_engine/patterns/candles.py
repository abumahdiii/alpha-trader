"""Reversal candle patterns: pin bar (hammer / shooting star) and engulfing.

Used as the *confirmation* step of the StdDev-channel setups (knowledge file section 4): a setup only
turns into a suggestion when a reversal candle in the trade direction closes at the channel line.

Definitions (per bar, all in price units)::

    body       = |close - open|
    upper_wick = high - max(open, close)
    lower_wick = min(open, close) - low
    range      = high - low

Bullish pin bar (hammer) at bar t -- all of:

* ``lower_wick >= wick_body_ratio * body``          (default ratio 2.0, inclusive)
* ``upper_wick <= opposite_wick_max * lower_wick``  (default 0.5, inclusive)
* ``lower_wick > 0`` and ``range > 0``
* ``lower_wick > upper_wick`` (dominance guard: makes "bullish AND bearish pin" impossible for *any*
  parameter set; with ``opposite_wick_max < 1`` it is already implied by the previous rules)
* if ``require_color``: ``close >= open``

Bearish pin bar (shooting star) is the exact mirror (upper wick dominant, ``close <= open`` if
``require_color``). A zero body (doji) is allowed: the ratio rule then holds whenever the dominant wick
is > 0, and the opposite-wick rule decides -- dragonfly doji -> bullish pin, gravestone -> bearish pin,
long-legged doji with equal wicks -> neither, flat bar (range 0) -> neither.

Bullish engulfing at bar t (uses bars t-1 and t only):

* bar t-1 bearish (``close < open``), bar t bullish (``close > open``)
* ``open_t <= close_{t-1}`` and ``close_t >= open_{t-1}`` (inclusive edges)
* ``body_t > body_{t-1}`` (``strict_body=True``, default: identical bodies do not count) or
  ``body_t >= body_{t-1}`` (``strict_body=False``)

Bearish engulfing is the mirror. A zero-body previous bar (doji) is neither bullish nor bearish, so it
can never be engulfed. The first bar has no previous bar and is always False.

Float tolerance: prices such as 2000.10 are not exactly representable, so "lower wick exactly 2x body"
can miss by ~1e-13. Every threshold comparison therefore carries an absolute tolerance
``tol = rel_tol * max(|high|, |low|, range, 1)`` (``rel_tol`` default 1e-9 -> 2e-6 at price 2000, far
below one point). Inclusive rules (>=, <=) accept values within ``tol`` of the limit; strict rules
(``> 0``, ``close > open``, ``body_t > body_{t-1}``) require clearing the limit by more than ``tol``,
so float noise is treated as zero. For engulfing the larger tolerance of the two bars is used.

No look-ahead: every output at bar t is a function of bars t (pin bar) or t-1 and t (engulfing) only.

Input: a DataFrame with ``open, high, low, close`` columns (extra columns such as ``time`` are ignored),
a mapping of those four names to 1-D arrays, or a 2-D numpy array of shape (n, 4) in OHLC order.
Output: boolean ``pandas.Series`` aligned with the input index (RangeIndex for non-DataFrame input).
Bars containing NaN never match. Structurally invalid bars (high below the body or low above it,
beyond ``tol``) raise ``ValueError`` -- bad data is reported, never silently turned into signals.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, is_dev_mode

logger = get_logger(__name__)

OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")
PATTERNS: tuple[str, ...] = ("pin_bar", "engulfing")
PATTERN_COLUMNS: dict[str, tuple[str, str]] = {
    "pin_bar": ("bullish_pin_bar", "bearish_pin_bar"),
    "engulfing": ("bullish_engulfing", "bearish_engulfing"),
}

DEFAULT_WICK_BODY_RATIO = 2.0
DEFAULT_OPPOSITE_WICK_MAX = 0.5
DEFAULT_REL_TOL = 1e-9

_PIN_PARAMS = frozenset({"wick_body_ratio", "opposite_wick_max", "require_color", "rel_tol"})
_ENGULF_PARAMS = frozenset({"strict_body", "rel_tol"})


# --------------------------------------------------------------------------------------------------
# Input handling and validation
# --------------------------------------------------------------------------------------------------
class _Bars:
    """Float64 OHLC arrays plus derived anatomy, computed once per call."""

    __slots__ = ("index", "open", "high", "low", "close", "body", "upper", "lower", "range", "tol")

    def __init__(self, index: pd.Index, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray,
                 rel_tol: float) -> None:
        self.index = index
        self.open, self.high, self.low, self.close = o, h, lo, c
        self.body = np.abs(c - o)
        self.range = h - lo
        self.tol = rel_tol * np.maximum.reduce([np.abs(h), np.abs(lo), self.range, np.ones_like(h)])
        top = np.maximum(o, c)
        bottom = np.minimum(o, c)
        upper = h - top
        lower = bottom - lo
        _check_structure(index, upper, lower, self.tol)
        # Clamp float noise within tolerance to zero; NaN propagates (np.maximum keeps NaN).
        self.upper = np.maximum(upper, 0.0)
        self.lower = np.maximum(lower, 0.0)

    def __len__(self) -> int:
        return len(self.open)


def _check_structure(index: pd.Index, upper: np.ndarray, lower: np.ndarray, tol: np.ndarray) -> None:
    with np.errstate(invalid="ignore"):
        bad = (upper < -tol) | (lower < -tol)  # NaN compares False -> NaN bars are not "bad"
    if bad.any():
        first = index[int(np.argmax(bad))]
        raise ValueError(
            f"invalid OHLC: {int(bad.sum())} bar(s) with high below max(open, close) or low above "
            f"min(open, close); first at index {first!r}"
        )


def _to_bars(data: Any, rel_tol: float) -> _Bars:
    if isinstance(data, pd.DataFrame):
        missing = [col for col in OHLC_COLUMNS if col not in data.columns]
        if missing:
            raise ValueError(f"missing OHLC column(s): {missing}")
        index = data.index
        arrays = [data[col].to_numpy(dtype=np.float64) for col in OHLC_COLUMNS]
    elif isinstance(data, Mapping):
        missing = [col for col in OHLC_COLUMNS if col not in data]
        if missing:
            raise ValueError(f"missing OHLC key(s): {missing}")
        arrays = [np.asarray(data[col], dtype=np.float64) for col in OHLC_COLUMNS]
        index = None
    elif isinstance(data, np.ndarray):
        arr = np.asarray(data, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != 4:
            raise ValueError(f"numpy input must have shape (n, 4) in OHLC order, got {arr.shape}")
        arrays = [np.ascontiguousarray(arr[:, i]) for i in range(4)]
        index = None
    else:
        raise TypeError(
            "expected a DataFrame, a mapping of open/high/low/close arrays, or an (n, 4) numpy array; "
            f"got {type(data).__name__}"
        )
    for name, arr in zip(OHLC_COLUMNS, arrays):
        if arr.ndim != 1:
            raise ValueError(f"'{name}' must be 1-D, got shape {arr.shape}")
        if len(arr) != len(arrays[0]):
            raise ValueError("open/high/low/close must all have the same length")
    if index is None:
        index = pd.RangeIndex(len(arrays[0]))
    return _Bars(index, *arrays, rel_tol=rel_tol)


def _check_number(name: str, value: Any, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if positive and value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    if not positive and value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return value


def _check_bool(name: str, value: Any) -> bool:
    if not isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be a bool, got {value!r}")
    return bool(value)


def _pin_params(wick_body_ratio: Any, opposite_wick_max: Any, require_color: Any,
                rel_tol: Any) -> tuple[float, float, bool, float]:
    return (
        _check_number("wick_body_ratio", wick_body_ratio, positive=True),
        _check_number("opposite_wick_max", opposite_wick_max, positive=False),
        _check_bool("require_color", require_color),
        _check_number("rel_tol", rel_tol, positive=False),
    )


def _normalize_patterns(patterns: Any) -> tuple[str, ...]:
    if isinstance(patterns, str):
        patterns = (patterns,)
    if not isinstance(patterns, Iterable):
        raise ValueError(f"patterns must be an iterable of pattern names, got {patterns!r}")
    requested = list(patterns)
    if not requested:
        raise ValueError(f"patterns must not be empty; choose from {PATTERNS}")
    unknown = [p for p in requested if p not in PATTERNS]
    if unknown:
        raise ValueError(f"unknown pattern(s) {unknown}; choose from {PATTERNS}")
    return tuple(p for p in PATTERNS if p in requested)  # canonical order, duplicates dropped


def _split_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    unknown = sorted(set(params) - _PIN_PARAMS - _ENGULF_PARAMS)
    if unknown:
        raise ValueError(
            f"unknown parameter(s) {unknown}; allowed: {sorted(_PIN_PARAMS | _ENGULF_PARAMS)}"
        )
    pin = {k: v for k, v in params.items() if k in _PIN_PARAMS}
    engulf = {k: v for k, v in params.items() if k in _ENGULF_PARAMS}
    return pin, engulf


# --------------------------------------------------------------------------------------------------
# Core vectorized masks (no logging; public wrappers log one summary line each)
# --------------------------------------------------------------------------------------------------
def _pin_masks(bars: _Bars, ratio: float, opp: float, require_color: bool) -> tuple[np.ndarray, np.ndarray]:
    b, up, lo, tol = bars.body, bars.upper, bars.lower, bars.tol
    with np.errstate(invalid="ignore"):
        has_range = bars.range > tol
        bull = (
            has_range
            & (lo > tol)
            & (lo >= ratio * b - tol)
            & (up <= opp * lo + tol)
            & (lo > up)
        )
        bear = (
            has_range
            & (up > tol)
            & (up >= ratio * b - tol)
            & (lo <= opp * up + tol)
            & (up > lo)
        )
        if require_color:
            bull &= bars.close >= bars.open - tol
            bear &= bars.close <= bars.open + tol
    return bull, bear


def _shift(values: np.ndarray) -> np.ndarray:
    out = np.empty_like(values)
    if len(values):
        out[0] = np.nan
        out[1:] = values[:-1]
    return out


def _engulf_masks(bars: _Bars, strict_body: bool) -> tuple[np.ndarray, np.ndarray]:
    o, c, body = bars.open, bars.close, bars.body
    po, pc, pbody = _shift(o), _shift(c), _shift(body)
    tol = np.fmax(bars.tol, _shift(bars.tol))  # fmax: first bar keeps its own tol, rest pairwise max
    with np.errstate(invalid="ignore"):
        if strict_body:
            bigger = body > pbody + tol
        else:
            bigger = body >= pbody - tol
        bull = (po - pc > tol) & (c - o > tol) & (o <= pc + tol) & (c >= po - tol) & bigger
        bear = (pc - po > tol) & (o - c > tol) & (o >= pc - tol) & (c <= po + tol) & bigger
    return bull, bear  # first bar: previous values are NaN -> False


def _series(mask: np.ndarray, bars: _Bars, name: str) -> pd.Series:
    return pd.Series(np.asarray(mask, dtype=bool), index=bars.index, name=name, dtype=bool)


def _log_summary(func: str, bars: _Bars, counts: dict[str, int], params: dict[str, Any]) -> None:
    if is_dev_mode():
        logger.debug("%s: bars=%d counts=%s params=%s", func, len(bars), counts, params)


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------
def candle_anatomy(data: Any, *, rel_tol: float = DEFAULT_REL_TOL) -> pd.DataFrame:
    """Per-bar ``body``, ``upper_wick``, ``lower_wick``, ``range`` (wicks clamped at 0)."""
    rel_tol = _check_number("rel_tol", rel_tol, positive=False)
    bars = _to_bars(data, rel_tol)
    return pd.DataFrame(
        {"body": bars.body, "upper_wick": bars.upper, "lower_wick": bars.lower, "range": bars.range},
        index=bars.index,
    )


def bullish_pin_bar(data: Any, *, wick_body_ratio: float = DEFAULT_WICK_BODY_RATIO,
                    opposite_wick_max: float = DEFAULT_OPPOSITE_WICK_MAX, require_color: bool = False,
                    rel_tol: float = DEFAULT_REL_TOL) -> pd.Series:
    """Bullish pin bar (hammer) per bar; see the module docstring for the exact rule."""
    ratio, opp, color, tol = _pin_params(wick_body_ratio, opposite_wick_max, require_color, rel_tol)
    bars = _to_bars(data, tol)
    bull, _ = _pin_masks(bars, ratio, opp, color)
    _log_summary("bullish_pin_bar", bars, {"bullish_pin_bar": int(bull.sum())},
                 {"wick_body_ratio": ratio, "opposite_wick_max": opp, "require_color": color})
    return _series(bull, bars, "bullish_pin_bar")


def bearish_pin_bar(data: Any, *, wick_body_ratio: float = DEFAULT_WICK_BODY_RATIO,
                    opposite_wick_max: float = DEFAULT_OPPOSITE_WICK_MAX, require_color: bool = False,
                    rel_tol: float = DEFAULT_REL_TOL) -> pd.Series:
    """Bearish pin bar (shooting star) per bar; mirror of :func:`bullish_pin_bar`."""
    ratio, opp, color, tol = _pin_params(wick_body_ratio, opposite_wick_max, require_color, rel_tol)
    bars = _to_bars(data, tol)
    _, bear = _pin_masks(bars, ratio, opp, color)
    _log_summary("bearish_pin_bar", bars, {"bearish_pin_bar": int(bear.sum())},
                 {"wick_body_ratio": ratio, "opposite_wick_max": opp, "require_color": color})
    return _series(bear, bars, "bearish_pin_bar")


def bullish_engulfing(data: Any, strict_body: bool = True, *,
                      rel_tol: float = DEFAULT_REL_TOL) -> pd.Series:
    """Bullish engulfing at bar t (bars t-1 and t); first bar always False."""
    strict = _check_bool("strict_body", strict_body)
    bars = _to_bars(data, _check_number("rel_tol", rel_tol, positive=False))
    bull, _ = _engulf_masks(bars, strict)
    _log_summary("bullish_engulfing", bars, {"bullish_engulfing": int(bull.sum())},
                 {"strict_body": strict})
    return _series(bull, bars, "bullish_engulfing")


def bearish_engulfing(data: Any, strict_body: bool = True, *,
                      rel_tol: float = DEFAULT_REL_TOL) -> pd.Series:
    """Bearish engulfing at bar t (bars t-1 and t); first bar always False."""
    strict = _check_bool("strict_body", strict_body)
    bars = _to_bars(data, _check_number("rel_tol", rel_tol, positive=False))
    _, bear = _engulf_masks(bars, strict)
    _log_summary("bearish_engulfing", bars, {"bearish_engulfing": int(bear.sum())},
                 {"strict_body": strict})
    return _series(bear, bars, "bearish_engulfing")


def _detect(data: Any, patterns: Any, params: dict[str, Any]) -> tuple[_Bars, dict[str, np.ndarray],
                                                                        dict[str, Any]]:
    enabled = _normalize_patterns(patterns)
    pin_kw, engulf_kw = _split_params(params)
    ratio, opp, color, rel_tol = _pin_params(
        pin_kw.get("wick_body_ratio", DEFAULT_WICK_BODY_RATIO),
        pin_kw.get("opposite_wick_max", DEFAULT_OPPOSITE_WICK_MAX),
        pin_kw.get("require_color", False),
        params.get("rel_tol", DEFAULT_REL_TOL),
    )
    strict = _check_bool("strict_body", engulf_kw.get("strict_body", True))
    bars = _to_bars(data, rel_tol)
    masks: dict[str, np.ndarray] = {}
    if "pin_bar" in enabled:
        masks["bullish_pin_bar"], masks["bearish_pin_bar"] = _pin_masks(bars, ratio, opp, color)
    if "engulfing" in enabled:
        masks["bullish_engulfing"], masks["bearish_engulfing"] = _engulf_masks(bars, strict)
    used = {"patterns": enabled, "wick_body_ratio": ratio, "opposite_wick_max": opp,
            "require_color": color, "strict_body": strict}
    return bars, masks, used


def _aggregate(masks: dict[str, np.ndarray], side: str, n: int) -> np.ndarray:
    out = np.zeros(n, dtype=bool)
    for name, mask in masks.items():
        if name.startswith(side):
            out |= mask
    return out


def bullish_reversal(data: Any, patterns: Any = PATTERNS, **params: Any) -> pd.Series:
    """True where any enabled bullish pattern fires (OR). ``params``: pin-bar and engulfing params."""
    bars, masks, used = _detect(data, patterns, params)
    bull = _aggregate(masks, "bullish", len(bars))
    counts = {k: int(v.sum()) for k, v in masks.items() if k.startswith("bullish")}
    counts["bullish"] = int(bull.sum())
    _log_summary("bullish_reversal", bars, counts, used)
    return _series(bull, bars, "bullish_reversal")


def bearish_reversal(data: Any, patterns: Any = PATTERNS, **params: Any) -> pd.Series:
    """True where any enabled bearish pattern fires (OR). ``params``: pin-bar and engulfing params."""
    bars, masks, used = _detect(data, patterns, params)
    bear = _aggregate(masks, "bearish", len(bars))
    counts = {k: int(v.sum()) for k, v in masks.items() if k.startswith("bearish")}
    counts["bearish"] = int(bear.sum())
    _log_summary("bearish_reversal", bars, counts, used)
    return _series(bear, bars, "bearish_reversal")


def detect_reversals(data: Any, patterns: Any = PATTERNS, **params: Any) -> pd.DataFrame:
    """All enabled patterns at once.

    Columns: one boolean column per enabled pattern and direction (``bullish_pin_bar``,
    ``bearish_pin_bar``, ``bullish_engulfing``, ``bearish_engulfing``), then ``bullish`` / ``bearish``
    (OR per direction) and ``pattern``: the names of the columns that fired, joined with ``"+"`` in
    that fixed order (``""`` when none). A bar can carry patterns of both directions (e.g. a bearish
    engulfing bar with a long lower wick); the strategy layer picks the side allowed by the channel.
    """
    bars, masks, used = _detect(data, patterns, params)
    n = len(bars)
    bull = _aggregate(masks, "bullish", n)
    bear = _aggregate(masks, "bearish", n)
    names = np.full(n, "", dtype=object)
    for name, mask in masks.items():
        joined = np.where(names == "", name, names + "+" + name)
        names = np.where(mask, joined, names)
    frame = pd.DataFrame({name: np.asarray(mask, dtype=bool) for name, mask in masks.items()},
                         index=bars.index)
    frame["bullish"] = bull
    frame["bearish"] = bear
    frame["pattern"] = pd.Series(names, index=bars.index, dtype=object)
    counts = {k: int(v.sum()) for k, v in masks.items()}
    counts.update(bullish=int(bull.sum()), bearish=int(bear.sum()))
    _log_summary("detect_reversals", bars, counts, used)
    return frame
