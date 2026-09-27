"""Touch rule of the StdDev-channel system: ``low <= L + tol and high >= L - tol`` (inclusive).

Worked example (unit): L = 2000.00, tol = ATR_H1 2.50 * 0.1 = 0.25.
low 2000.25 -> touch (exactly tol); low 2000.25 + 1e-9 -> no touch.
high 1999.75 -> touch from below; high 1999.75 - 1e-9 -> no touch.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategies.stddev_channel.setups import any_touch, touch_offset, touches
from alpha_engine.strategy.base import evaluate_checked
from fixtures.channel_scenarios import DEFAULT_T, boundary_scenario, build

STRATEGY = StdDevChannelStrategy()
L, TOL = 2000.0, 2.5 * 0.1


def test_tolerance_worked_example_is_exact_in_float() -> None:
    assert TOL == 0.25 and L + TOL == 2000.25 and L - TOL == 1999.75


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (2000.25, 2003.0, True),            # low exactly tol above the line (inclusive)
        (2000.25 + 1e-9, 2003.0, False),    # tol + epsilon -> excluded
        (1995.0, 1999.75, True),            # high exactly tol below the line
        (1995.0, 1999.75 - 1e-9, False),
        (1999.0, 2001.0, True),             # range straddles the line
        (2000.0, 2000.0, True),             # zero-range bar on the line
        (2000.1, 2000.2, True),             # inside the tolerance band, above
        (1999.8, 1999.9, True),             # inside the tolerance band, below
    ],
)
def test_touch_rule_boundaries(low: float, high: float, expected: bool) -> None:
    assert touches(low, high, L, TOL) is expected


def test_zero_tolerance_requires_the_range_to_contain_the_line() -> None:
    assert touches(2000.0, 2001.0, L, 0.0)
    assert not touches(2000.0 + 1e-9, 2001.0, L, 0.0)


def test_nan_line_or_tolerance_never_touches() -> None:
    assert not touches(1999.0, 2001.0, math.nan, TOL)
    assert not touches(1999.0, 2001.0, L, math.nan)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_boundary_touch_inside_the_strategy(side: str) -> None:
    """Integration: the confirmation bar's extreme at exactly line +/- tol_t fires; + 1e-6 does not.

    The fixture keeps ATR_H1(t) independent of the touching extreme (gap bar before t), so tol_t is the
    same number in both runs (asserted).
    """
    sc, tol = boundary_scenario(side, 0.0)
    cb = sc.bars()
    t = DEFAULT_T
    line = sc.support()
    L_t = sc.line(line, t)
    extreme = cb.low[t] if side == "buy" else cb.high[t]
    assert cb.tol[t] == tol
    assert extreme == (L_t + tol if side == "buy" else L_t - tol)  # distance exactly tol
    assert touch_offset(cb, line, t, 1) == 0
    assert touch_offset(cb, line, t - 1, 0) is None  # bar t-1 is entirely beyond the line
    cand = evaluate_checked(STRATEGY, sc.ctx())
    assert cand is not None
    assert cand.setup.value == ("bounce_lower" if side == "buy" else "bounce_upper")
    assert cand.extra["touch_offset"] == 0 and cand.extra["touch_tol"] == tol

    sc2, tol2 = boundary_scenario(side, 1e-6)
    cb2 = sc2.bars()
    assert tol2 == tol and cb2.tol[t] == tol  # the tolerance did not move with the extreme
    assert touch_offset(cb2, line, t, 1) is None
    assert evaluate_checked(STRATEGY, sc2.ctx()) is None


def test_each_bar_is_judged_with_its_own_atr() -> None:
    """Touch at t-1 uses tol_{t-1} (the ATR known when that bar closed), not tol_t."""
    sc = build("up", "buy", "bounce_outer", touch_at=1)
    cb = sc.bars()
    t = DEFAULT_T
    assert touch_offset(cb, "lower", t, 1) == 1
    cand = evaluate_checked(STRATEGY, sc.ctx())
    assert cand is not None
    assert cand.extra["touch_bar_tol"] == pytest.approx(cb.tol[t - 1], rel=0, abs=0)
    assert cand.extra["touch_bar_tol"] != cand.extra["touch_tol"]


def test_touch_lookback_window() -> None:
    sc = build("up", "buy", "bounce_outer", touch_at=2)
    cb = sc.bars()
    t = DEFAULT_T
    assert touch_offset(cb, "lower", t, 1) is None
    assert touch_offset(cb, "lower", t, 2) == 2
    assert not any_touch(cb, t, 1) and any_touch(cb, t, 2)


def test_tolerance_scales_with_param() -> None:
    sc = build("up", "buy", "bounce_outer")
    cb = sc.bars()
    sc2 = build("up", "buy", "bounce_outer", params={"touch_atr_mult": 0.3})
    cb2 = sc2.bars()
    finite = np.isfinite(cb.atr_h1)
    np.testing.assert_array_equal(cb.tol[finite], cb.atr_h1[finite] * 0.1)
    np.testing.assert_array_equal(cb2.tol[finite], cb2.atr_h1[finite] * 0.3)
