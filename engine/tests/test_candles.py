"""Reversal candle patterns (phase 2, group 2C).

Every hand-built candle states its numbers; comments show the anatomy they produce
(body / upper wick / lower wick, in price units). Prices around 2000 mimic XAUUSD (1 point = 0.01).
"""

from __future__ import annotations

import io
import logging
import math

import numpy as np
import pandas as pd
import pytest

from alpha_engine.logging_setup import configure_logging
from alpha_engine.patterns import (
    bearish_engulfing,
    bearish_pin_bar,
    bearish_reversal,
    bullish_engulfing,
    bullish_pin_bar,
    bullish_reversal,
    candle_anatomy,
    detect_reversals,
)
from alpha_engine.patterns import candles as candles_mod


def frame(*rows: tuple[float, float, float, float]) -> pd.DataFrame:
    """Rows are (open, high, low, close)."""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], dtype="float64")


def pins(*rows, **params) -> tuple[list[bool], list[bool]]:
    df = frame(*rows)
    return bullish_pin_bar(df, **params).tolist(), bearish_pin_bar(df, **params).tolist()


def engulfs(*rows, **params) -> tuple[list[bool], list[bool]]:
    df = frame(*rows)
    return bullish_engulfing(df, **params).tolist(), bearish_engulfing(df, **params).tolist()


# --------------------------------------------------------------------------------------------------
# Pin bars
# --------------------------------------------------------------------------------------------------
def test_normal_hammer():
    # O 100.0 H 100.6 L 98.5 C 100.5 -> body 0.5, upper 0.1, lower 1.5 (= 3.0x body; 0.1 <= 0.75)
    assert pins((100.0, 100.6, 98.5, 100.5)) == ([True], [False])


def test_normal_shooting_star():
    # O 100.5 H 102.0 L 99.9 C 100.0 -> body 0.5, upper 1.5 (3x), lower 0.1 (<= 0.75)
    assert pins((100.5, 102.0, 99.9, 100.0)) == ([False], [True])


def test_ordinary_candle_is_no_pin():
    # O 100 H 101.2 L 99.8 C 101 -> body 1.0, upper 0.2, lower 0.2
    assert pins((100.0, 101.2, 99.8, 101.0)) == ([False], [False])


def test_anatomy_values():
    got = candle_anatomy(frame((2000.10, 2000.30, 1999.90, 2000.20))).iloc[0]
    assert got["body"] == pytest.approx(0.10)
    assert got["upper_wick"] == pytest.approx(0.10)
    assert got["lower_wick"] == pytest.approx(0.20)
    assert got["range"] == pytest.approx(0.40)


def test_dragonfly_doji_is_bullish_pin():
    # O=C=H 2000.00, L 1999.00 -> body 0, upper 0, lower 1.00
    assert pins((2000.0, 2000.0, 1999.0, 2000.0)) == ([True], [False])


def test_gravestone_doji_is_bearish_pin():
    # O=C=L 2000.00, H 2001.00 -> body 0, upper 1.00, lower 0
    assert pins((2000.0, 2001.0, 2000.0, 2000.0)) == ([False], [True])


def test_long_legged_doji_equal_wicks_is_neither():
    # O=C 2000, H 2001, L 1999 -> body 0, both wicks 1.00; 1.00 <= 0.5*1.00 fails both ways
    assert pins((2000.0, 2001.0, 1999.0, 2000.0)) == ([False], [False])


def test_flat_bar_range_zero_is_neither():
    assert pins((2000.0, 2000.0, 2000.0, 2000.0)) == ([False], [False])


def test_doji_with_opposite_wick_at_half_is_pin():
    # O=C 2000, H 2000.50, L 1999.00 -> body 0, upper 0.50 = 0.5 * lower 1.00 (inclusive)
    assert pins((2000.0, 2000.5, 1999.0, 2000.0)) == ([True], [False])


def test_wick_exactly_twice_body_is_included_despite_float_error():
    # O 2000.10 H 2000.20 L 1999.90 C 2000.20 -> body 0.10, upper 0, lower 0.20 = 2 x body
    o, h, lo, c = 2000.10, 2000.20, 1999.90, 2000.20
    body, lower = abs(c - o), min(o, c) - lo
    assert lower < 2.0 * body  # raw float64 says "just below" (0.19999999999982 < 0.20000000000027)
    assert pins((o, h, lo, c)) == ([True], [False])
    # With the tolerance switched off the representation error wins -> excluded.
    assert pins((o, h, lo, c), rel_tol=0.0) == ([False], [False])


def test_wick_one_point_below_threshold_is_excluded():
    # L 1999.91 -> lower 0.19 < 2 x 0.10
    assert pins((2000.10, 2000.20, 1999.91, 2000.20)) == ([False], [False])


def test_shooting_star_wick_exactly_threshold_and_one_point_below():
    # O 2000.20 H 2000.40 L 2000.10 C 2000.10 -> body 0.10, upper 0.20 = 2x, lower 0
    assert pins((2000.20, 2000.40, 2000.10, 2000.10)) == ([False], [True])
    # H 2000.39 -> upper 0.19 -> excluded
    assert pins((2000.20, 2000.39, 2000.10, 2000.10)) == ([False], [False])


def test_opposite_wick_exactly_at_half_limit():
    # O 2000.10 H 2000.40 L 1999.70 C 2000.20 -> body 0.10, upper 0.20 = 0.5 x lower 0.40 -> included
    assert pins((2000.10, 2000.40, 1999.70, 2000.20)) == ([True], [False])
    # H 2000.41 -> upper 0.21 > 0.20 -> excluded
    assert pins((2000.10, 2000.41, 1999.70, 2000.20)) == ([False], [False])
    # mirror: O 2000.20 H 2000.60 L 1999.90 C 2000.10 -> body 0.10, upper 0.40, lower 0.20 = 0.5x
    assert pins((2000.20, 2000.60, 1999.90, 2000.10)) == ([False], [True])
    # L 1999.89 -> lower 0.21 > 0.20 -> excluded
    assert pins((2000.20, 2000.60, 1999.89, 2000.10)) == ([False], [False])


def test_custom_ratio_and_opposite_limit():
    # body 0.5, lower 1.5 (3x), upper 0.1: ratio 3.0 inclusive -> pin; ratio 3.01 -> not
    row = (100.0, 100.6, 98.5, 100.5)
    assert pins(row, wick_body_ratio=3.0)[0] == [True]
    assert pins(row, wick_body_ratio=3.01)[0] == [False]
    # opposite_wick_max 0 -> any upper wick disqualifies; 0.1/1.5 = 0.0667
    assert pins(row, opposite_wick_max=0.0)[0] == [False]
    assert pins(row, opposite_wick_max=0.07)[0] == [True]
    assert pins((2000.0, 2000.0, 1999.0, 2000.0), opposite_wick_max=0.0)[0] == [True]


def test_require_color_toggle():
    red_hammer = (100.5, 100.6, 98.5, 100.0)  # body 0.5 (bearish), upper 0.1, lower 1.5
    green_star = (100.0, 102.0, 99.9, 100.5)  # body 0.5 (bullish), upper 1.5, lower 0.1
    dragonfly = (2000.0, 2000.0, 1999.0, 2000.0)
    gravestone = (2000.0, 2001.0, 2000.0, 2000.0)
    assert pins(red_hammer) == ([True], [False])
    assert pins(red_hammer, require_color=True) == ([False], [False])
    assert pins(green_star) == ([False], [True])
    assert pins(green_star, require_color=True) == ([False], [False])
    # doji (close == open) satisfies close >= open and close <= open
    assert pins(dragonfly, require_color=True) == ([True], [False])
    assert pins(gravestone, require_color=True) == ([False], [True])
    # green hammer keeps firing
    assert pins((100.0, 100.6, 98.5, 100.5), require_color=True) == ([True], [False])


def test_never_both_even_with_permissive_opposite_limit():
    # Equal wicks (1.00 each), zero body. opposite_wick_max=1.0 would admit both directions without
    # the dominance guard; with it, neither fires.
    long_legged = (2000.0, 2001.0, 1999.0, 2000.0)
    assert pins(long_legged, opposite_wick_max=1.0) == ([False], [False])
    assert pins(long_legged, opposite_wick_max=5.0) == ([False], [False])
    # Slightly dominant lower wick (1.00 vs 0.99) -> bullish only.
    assert pins((2000.0, 2000.99, 1999.0, 2000.0), opposite_wick_max=1.0) == ([True], [False])


@pytest.mark.parametrize("params", [
    {},
    {"opposite_wick_max": 1.0},
    {"opposite_wick_max": 3.0, "wick_body_ratio": 0.1},
    {"require_color": True},
])
def test_never_both_directions_random(params):
    df = random_bars(30_000, seed=7)
    bull = bullish_pin_bar(df, **params)
    bear = bearish_pin_bar(df, **params)
    assert bull.sum() > 0 and bear.sum() > 0
    assert not (bull & bear).any()


def test_nan_bar_never_matches():
    df = frame((2000.0, 2000.0, 1999.0, 2000.0), (np.nan, 2000.0, 1999.0, 2000.0),
               (2000.0, 2000.0, 1999.0, 2000.0))
    assert bullish_pin_bar(df).tolist() == [True, False, True]
    assert detect_reversals(df)["pattern"].tolist()[1] == ""


# --------------------------------------------------------------------------------------------------
# Engulfing
# --------------------------------------------------------------------------------------------------
BEAR_PREV = (2000.50, 2000.60, 1999.90, 2000.00)  # bearish, body 0.50
BULL_PREV = (2000.00, 2000.60, 1999.90, 2000.50)  # bullish, body 0.50


def test_bullish_engulfing():
    # cur O 1999.90 C 2000.70 (body 0.80): 1999.90 <= 2000.00 and 2000.70 >= 2000.50
    assert engulfs(BEAR_PREV, (1999.90, 2000.75, 1999.85, 2000.70)) == ([False, True], [False, False])


def test_bearish_engulfing():
    # cur O 2000.60 C 1999.80 (body 0.80): 2000.60 >= 2000.50 and 1999.80 <= 2000.00
    assert engulfs(BULL_PREV, (2000.60, 2000.65, 1999.75, 1999.80)) == ([False, False], [False, True])


def test_identical_bodies_do_not_engulf_unless_not_strict():
    # prev O 2000.50 C 2000.00; cur O 2000.00 C 2000.50 -> both bodies 0.50, edges equal
    rows = (BEAR_PREV, (2000.00, 2000.55, 1999.95, 2000.50))
    assert engulfs(*rows) == ([False, False], [False, False])
    assert engulfs(*rows, strict_body=False) == ([False, True], [False, False])
    rows = (BULL_PREV, (2000.50, 2000.55, 1999.95, 2000.00))
    assert engulfs(*rows) == ([False, False], [False, False])
    assert engulfs(*rows, strict_body=False) == ([False, False], [False, True])


def test_previous_doji_is_never_engulfed():
    doji = (2000.00, 2000.20, 1999.80, 2000.00)  # body 0
    assert engulfs(doji, (1999.90, 2000.35, 1999.85, 2000.30)) == ([False, False], [False, False])
    assert engulfs(doji, (2000.10, 2000.15, 1999.65, 1999.70)) == ([False, False], [False, False])
    assert engulfs(doji, (1999.90, 2000.35, 1999.85, 2000.30), strict_body=False)[0] == [False, False]


def test_current_doji_never_engulfs():
    assert engulfs(BEAR_PREV, (2000.20, 2000.80, 1999.80, 2000.20)) == ([False, False], [False, False])


def test_engulf_with_exactly_equal_edges():
    # open_t == close_{t-1} (2000.00), close_t 2000.60 > 2000.50, body 0.60 > 0.50
    assert engulfs(BEAR_PREV, (2000.00, 2000.65, 1999.95, 2000.60))[0] == [False, True]
    # close_t == open_{t-1} (2000.50), open_t 1999.90, body 0.60
    assert engulfs(BEAR_PREV, (1999.90, 2000.55, 1999.85, 2000.50))[0] == [False, True]
    # open_t one point above prev close (2000.01) -> not engulfing
    assert engulfs(BEAR_PREV, (2000.01, 2000.65, 1999.95, 2000.60))[0] == [False, False]
    # close_t one point below prev open (2000.49) -> not engulfing
    assert engulfs(BEAR_PREV, (1999.90, 2000.55, 1999.85, 2000.49))[0] == [False, False]
    # bearish mirror: open_t == close_{t-1} (2000.50), close_t 1999.90
    assert engulfs(BULL_PREV, (2000.50, 2000.55, 1999.85, 1999.90))[1] == [False, True]


def test_same_color_does_not_engulf():
    # prev bullish, cur bigger bullish -> no bullish engulfing (prev must be bearish)
    assert engulfs(BULL_PREV, (1999.90, 2000.75, 1999.85, 2000.70)) == ([False, False], [False, False])


def test_first_bar_is_always_false():
    assert engulfs((1999.90, 2000.75, 1999.85, 2000.70)) == ([False], [False])
    assert engulfs((2000.60, 2000.65, 1999.75, 1999.80)) == ([False], [False])
    empty = frame()
    assert bullish_engulfing(empty).tolist() == []
    assert detect_reversals(empty).shape[0] == 0


# --------------------------------------------------------------------------------------------------
# Aggregation, naming, inputs
# --------------------------------------------------------------------------------------------------
def test_detect_reversals_columns_and_pattern_names():
    idx = pd.date_range("2026-01-05 00:00", periods=6, freq="h", tz="UTC")
    df = frame(
        BEAR_PREV,                                   # 0: nothing
        (1999.90, 2000.65, 1998.00, 2000.60),        # 1: body 0.70, lower 1.90, upper 0.05 + engulf
        (2000.00, 2000.60, 1999.90, 2000.50),        # 2: plain bullish (upper 0.1, lower 0.1)
        (2000.60, 2000.65, 1998.00, 1999.90),        # 3: bearish engulf AND lower 1.90 >= 1.40 pin
        (2000.0, 2001.0, 2000.0, 2000.0),            # 4: gravestone
        (2000.0, 2000.0, 2000.0, 2000.0),            # 5: flat
    )
    df.index = idx
    df["time"] = idx  # extra column is ignored
    out = detect_reversals(df)
    assert list(out.columns) == ["bullish_pin_bar", "bearish_pin_bar", "bullish_engulfing",
                                 "bearish_engulfing", "bullish", "bearish", "pattern"]
    assert out.index.equals(idx)
    assert out["pattern"].tolist() == [
        "",
        "bullish_pin_bar+bullish_engulfing",
        "",
        "bullish_pin_bar+bearish_engulfing",
        "bearish_pin_bar",
        "",
    ]
    assert out["bullish"].tolist() == [False, True, False, True, False, False]
    assert out["bearish"].tolist() == [False, False, False, True, True, False]
    for col in ["bullish_pin_bar", "bearish_pin_bar", "bullish_engulfing", "bearish_engulfing",
                "bullish", "bearish"]:
        assert out[col].dtype == bool

    assert bullish_reversal(df).tolist() == out["bullish"].tolist()
    assert bearish_reversal(df).tolist() == out["bearish"].tolist()
    assert bullish_reversal(df).index.equals(idx)

    only_engulf = detect_reversals(df, patterns=("engulfing",))
    assert list(only_engulf.columns) == ["bullish_engulfing", "bearish_engulfing", "bullish",
                                         "bearish", "pattern"]
    assert only_engulf["pattern"].tolist()[1] == "bullish_engulfing"
    assert only_engulf["bearish"].tolist() == [False, False, False, True, False, False]
    assert bearish_reversal(df, patterns="pin_bar").tolist() == [False, False, False, False, True, False]
    # duplicates / order do not matter
    assert detect_reversals(df, patterns=["engulfing", "pin_bar", "engulfing"]).equals(out)


def test_reversal_params_are_forwarded():
    df = frame(BEAR_PREV, (2000.00, 2000.55, 1999.95, 2000.50), (100.5, 100.6, 98.5, 100.0))
    assert bullish_reversal(df).tolist() == [False, False, True]
    assert bullish_reversal(df, strict_body=False).tolist() == [False, True, True]
    assert bullish_reversal(df, require_color=True).tolist() == [False, False, False]
    assert bullish_reversal(df, wick_body_ratio=3.01).tolist() == [False, False, False]


def test_numpy_and_mapping_inputs_match_dataframe():
    df = random_bars(500, seed=3)
    expected = detect_reversals(df).reset_index(drop=True)
    arr = df[["open", "high", "low", "close"]].to_numpy()
    mapping = {k: df[k].to_numpy() for k in ("open", "high", "low", "close")}
    pd.testing.assert_frame_equal(detect_reversals(arr), expected)
    pd.testing.assert_frame_equal(detect_reversals(mapping), expected)
    assert bullish_pin_bar(arr).tolist() == expected["bullish_pin_bar"].tolist()


def test_input_validation():
    with pytest.raises(ValueError, match="missing OHLC column"):
        bullish_pin_bar(pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0]}))
    with pytest.raises(ValueError, match="invalid OHLC"):
        bullish_pin_bar(frame((2000.0, 2000.0, 1999.0, 2000.5)))  # high below close
    with pytest.raises(ValueError, match="invalid OHLC"):
        bearish_engulfing(frame((2000.0, 2001.0, 2000.2, 2000.5)))  # low above open
    with pytest.raises(ValueError, match="shape"):
        bullish_pin_bar(np.zeros((3, 5)))
    with pytest.raises(ValueError, match="same length"):
        bullish_pin_bar({"open": [1.0, 1.0], "high": [1.0], "low": [1.0], "close": [1.0]})
    with pytest.raises(TypeError):
        bullish_pin_bar([[1.0, 1.0, 1.0, 1.0]])


@pytest.mark.parametrize("kwargs", [
    {"wick_body_ratio": 0},
    {"wick_body_ratio": -1.0},
    {"wick_body_ratio": math.nan},
    {"wick_body_ratio": math.inf},
    {"wick_body_ratio": "2"},
    {"wick_body_ratio": True},
    {"opposite_wick_max": -0.01},
    {"opposite_wick_max": math.nan},
    {"require_color": "yes"},
    {"require_color": 1},
    {"rel_tol": -1e-9},
])
def test_pin_param_validation(kwargs):
    df = frame((100.0, 100.6, 98.5, 100.5))
    with pytest.raises(ValueError):
        bullish_pin_bar(df, **kwargs)
    with pytest.raises(ValueError):
        bearish_pin_bar(df, **kwargs)
    with pytest.raises(ValueError):
        detect_reversals(df, **kwargs)


def test_engulf_and_pattern_validation():
    df = frame(BEAR_PREV)
    with pytest.raises(ValueError):
        bullish_engulfing(df, strict_body=1)
    with pytest.raises(ValueError):
        bearish_engulfing(df, strict_body="no")
    for bad in [(), ("pin",), ("pin_bar", "hammer"), 3, None]:
        with pytest.raises(ValueError):
            bullish_reversal(df, patterns=bad)
        with pytest.raises(ValueError):
            detect_reversals(df, patterns=bad)
    with pytest.raises(ValueError, match="unknown parameter"):
        detect_reversals(df, wick_ratio=2.0)
    with pytest.raises(ValueError):
        bearish_reversal(df, strict_body=None)
    # boundary values that are allowed
    bullish_pin_bar(df, opposite_wick_max=0.0, wick_body_ratio=1e-6, rel_tol=0.0)
    detect_reversals(df, patterns="engulfing", strict_body=False)


# --------------------------------------------------------------------------------------------------
# No look-ahead
# --------------------------------------------------------------------------------------------------
def test_no_look_ahead_when_bars_are_appended_or_future_changes():
    full = random_bars(2_000, seed=11)
    head = full.iloc[:1_200]
    on_head = detect_reversals(head)
    on_full = detect_reversals(full)
    pd.testing.assert_frame_equal(on_full.iloc[:1_200], on_head)

    # Rewriting every future bar (t >= 1200) leaves results at t < 1200 unchanged.
    changed = full.copy()
    changed.iloc[1_200:, :] = changed.iloc[1_200:, :].to_numpy()[::-1]
    pd.testing.assert_frame_equal(detect_reversals(changed).iloc[:1_200], on_head)

    # Bar-by-bar replay: the verdict on the last closed bar equals the batch verdict.
    for t in range(1_150, 1_200):
        step = detect_reversals(full.iloc[: t + 1]).iloc[-1]
        assert step.equals(on_full.iloc[t])


def test_engulfing_ignores_next_bar():
    rows = [BEAR_PREV, (1999.90, 2000.75, 1999.85, 2000.70)]
    base = bullish_engulfing(frame(*rows)).tolist()
    with_next = bullish_engulfing(frame(*rows, (2003.0, 2004.0, 1990.0, 1991.0))).tolist()
    assert with_next[:2] == base == [False, True]


# --------------------------------------------------------------------------------------------------
# Vectorized == naive per-bar reference
# --------------------------------------------------------------------------------------------------
def random_bars(n: int, seed: int) -> pd.DataFrame:
    """Integer-point bars at ~2000 (1 point = 0.01) that hit the pattern boundaries often:
    zero bodies, wick == k x body, opposite wick == half, open_t == close_{t-1}, flat bars."""
    rng = np.random.default_rng(seed)
    o = np.empty(n, dtype=np.int64)
    c = np.empty(n, dtype=np.int64)
    hi = np.empty(n, dtype=np.int64)
    lo = np.empty(n, dtype=np.int64)
    prev_close = 200_000
    for i in range(n):
        gap = 0 if rng.random() < 0.6 else int(rng.integers(-5, 6))
        op = prev_close + gap
        body = 0 if rng.random() < 0.15 else int(rng.integers(1, 60))
        if rng.random() < 0.3 and i > 0:
            body = abs(int(c[i - 1] - o[i - 1])) + int(rng.integers(-1, 2))  # near-equal bodies
            body = max(body, 0)
        cl = op + body if rng.random() < 0.5 else op - body

        def wick(ref: int) -> int:
            r = rng.random()
            if r < 0.2:
                return 0
            if r < 0.45:
                return max(2 * body + int(rng.integers(-1, 2)), 0)  # around the 2x threshold
            if r < 0.6:
                return ref // 2 if ref else 0
            return int(rng.integers(0, 150))

        lower = wick(0)
        upper = wick(lower)
        if rng.random() < 0.3:
            upper, lower = lower, wick(lower)  # let the opposite side sit at the half limit too
        if rng.random() < 0.01:
            cl, upper, lower = op, 0, 0  # flat bar
        o[i], c[i] = op, cl
        hi[i] = max(op, cl) + upper
        lo[i] = min(op, cl) - lower
        prev_close = cl
    return pd.DataFrame({"open": o / 100, "high": hi / 100, "low": lo / 100, "close": c / 100})


def naive_patterns(df: pd.DataFrame, wick_body_ratio=2.0, opposite_wick_max=0.5,
                   require_color=False, strict_body=True, rel_tol=1e-9) -> pd.DataFrame:
    rows = list(df[["open", "high", "low", "close"]].itertuples(index=False, name=None))
    out = {k: [] for k in ("bullish_pin_bar", "bearish_pin_bar", "bullish_engulfing",
                           "bearish_engulfing")}

    def anatomy(o, h, low, c):
        body = abs(c - o)
        upper = max(h - max(o, c), 0.0)
        lower = max(min(o, c) - low, 0.0)
        rng = h - low
        tol = rel_tol * max(abs(h), abs(low), rng, 1.0)
        return body, upper, lower, rng, tol

    for i, (o, h, low, c) in enumerate(rows):
        body, up, lw, rng, tol = anatomy(o, h, low, c)
        bull = (rng > tol and lw > tol and lw >= wick_body_ratio * body - tol
                and up <= opposite_wick_max * lw + tol and lw > up)
        bear = (rng > tol and up > tol and up >= wick_body_ratio * body - tol
                and lw <= opposite_wick_max * up + tol and up > lw)
        if require_color:
            bull = bull and c >= o - tol
            bear = bear and c <= o + tol
        out["bullish_pin_bar"].append(bool(bull))
        out["bearish_pin_bar"].append(bool(bear))

        bull_e = bear_e = False
        if i > 0:
            po, ph, pl, pc = rows[i - 1]
            pbody, _, _, _, ptol = anatomy(po, ph, pl, pc)
            t = max(tol, ptol)
            bigger = body > pbody + t if strict_body else body >= pbody - t
            bull_e = (po - pc > t) and (c - o > t) and o <= pc + t and c >= po - t and bigger
            bear_e = (pc - po > t) and (o - c > t) and o >= pc - t and c <= po + t and bigger
        out["bullish_engulfing"].append(bool(bull_e))
        out["bearish_engulfing"].append(bool(bear_e))
    return pd.DataFrame(out, index=df.index)


@pytest.mark.parametrize("params", [
    {},
    {"require_color": True},
    {"wick_body_ratio": 1.0, "opposite_wick_max": 0.25},
    {"wick_body_ratio": 3.0, "opposite_wick_max": 1.0, "strict_body": False},
])
def test_vectorized_equals_naive_on_30k_random_bars(params):
    df = random_bars(30_000, seed=20260926)
    expected = naive_patterns(df, **params)
    got = detect_reversals(df, **params)
    for col in expected.columns:
        assert expected[col].sum() > 50, f"random data too thin for {col}"
        mismatch = np.flatnonzero(got[col].to_numpy() != expected[col].to_numpy())
        assert mismatch.size == 0, f"{col}: first mismatches at {mismatch[:5]}"
    assert (got["bullish"] == (expected["bullish_pin_bar"] | expected["bullish_engulfing"])).all()
    assert (got["bearish"] == (expected["bearish_pin_bar"] | expected["bearish_engulfing"])).all()


def test_random_bars_hit_exact_threshold_cases():
    """Guard that the 30k fixture really contains 'wick exactly 2x body' bars (tolerance matters)."""
    df = random_bars(30_000, seed=20260926)
    body_pts = (df["close"] - df["open"]).abs().mul(100).round()
    lower_pts = (df[["open", "close"]].min(axis=1) - df["low"]).mul(100).round()
    exact = (lower_pts == 2 * body_pts) & (body_pts > 0)
    assert exact.sum() > 100
    strict_float = bullish_pin_bar(df, rel_tol=0.0)
    tolerant = bullish_pin_bar(df)
    assert (tolerant & ~strict_float).sum() > 0  # float noise would have dropped real pins


# --------------------------------------------------------------------------------------------------
# Logging (01_logging_standard.md): one summary line per call, only under DEV_MODE
# --------------------------------------------------------------------------------------------------
class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def candle_log():
    handler = _ListHandler()
    candles_mod.logger.addHandler(handler)
    yield handler
    candles_mod.logger.removeHandler(handler)


def test_summary_log_under_dev_mode(make_settings, candle_log):
    configure_logging(make_settings("DEV_MODE=true\n"), stream=io.StringIO(), force=True)
    df = random_bars(1_000, seed=5)
    out = detect_reversals(df)
    assert len(candle_log.records) == 1  # no per-bar spam
    msg = candle_log.records[0].getMessage()
    assert msg.startswith("detect_reversals: bars=1000")
    assert f"'bullish_pin_bar': {int(out['bullish_pin_bar'].sum())}" in msg
    assert f"'bearish_engulfing': {int(out['bearish_engulfing'].sum())}" in msg
    bullish_pin_bar(df)
    bearish_engulfing(df)
    assert len(candle_log.records) == 3


def test_no_log_when_dev_mode_off(make_settings, candle_log):
    configure_logging(make_settings("DEV_MODE=false\n"), stream=io.StringIO(), force=True)
    detect_reversals(random_bars(200, seed=5))
    bullish_reversal(random_bars(200, seed=5))
    assert candle_log.records == []
