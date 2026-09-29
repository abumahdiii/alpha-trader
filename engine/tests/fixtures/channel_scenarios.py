"""Deterministic synthetic H4/H1 data for the StdDev-channel strategy tests (no MT5).

H4 channel
----------
``make_h4(trend)`` builds ``N_H4`` continuous H4 bars from 2026-01-05 00:00 UTC with closes on a known
line plus an alternating residual::

    close_j = 2000 + s * j + 5 * (+1 if j even else -1),   s = +1.0 (up), -1.0 (down), 0.02 (flat)
    open_j = close_{j-1}, high = max(o, c) + 1, low = min(o, c) - 1

With n = 100 the fitted sigma is ~5 (bands ~mid +/- 10) and Wilder ATR_H4 is ~11, so
``|slope| * n`` is ~100 (up/down, clearly not flat) or ~2 (flat < 11).

H1 bars
-------
4 H1 bars per H4 bar (440 bars). The channel is usable from H1 index 399 (first H1 bar whose decision
time sees 100 closed H4 bars). Channel line values at an H1 bar depend only on the H4 data and the H1
open times, NOT on H1 prices, so scenarios read the line values first and then place candles relative to
them. Only ATR_H1 (touch tolerance, SL buffer) depends on the H1 prices.

Candles are described in "signed" offsets relative to a reference price for the BUY side; the SELL side
is the exact mirror (``price = ref - offset``, high/low swapped), so every buy scenario has a sell twin.

Background bars (no pattern, no touch): body 0.5, wicks 0.2 around ``ref`` (bullish for buy, bearish
for sell). A hammer (bullish pin bar) at line ``L``: low = L - 0.05, open = L + 1.95, close = L + 2.35,
high = L + 2.55 (lower wick 2.0 = 5x body, upper wick 0.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel.params import SCHEMA, resolve_params
from alpha_engine.strategies.stddev_channel.setups import ChannelBars, compute_channel_bars
from alpha_engine.strategy.base import H4_DURATION, StrategyContext, slice_closed_bars

T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")  # Monday
N_H4 = 110
N_H1 = 4 * N_H4
FIRST_VALID_H1 = 399
DEFAULT_T = 430
SLOPES = {"up": 1.0, "down": -1.0, "flat": 0.02}
AMP = 5.0
SYMBOL = "XAUUSD.x"

# Offsets (buy side) relative to a reference price: (open, high, low, close).
BG = (-0.25, 0.45, -0.45, 0.25)
HAMMER = (1.95, 2.55, -0.05, 2.35)          # bullish pin touching the reference line with its low
PLAIN_TOUCH = (0.30, 0.90, -0.05, 0.70)     # touches, but is not a reversal pattern
BREAKOUT = (-1.0, 1.7, -1.2, 1.5)           # closes 1.5 beyond the line, came from the other side


def make_h4(trend: str, n_h4: int = N_H4, base: float = 2000.0, amp: float = AMP) -> pd.DataFrame:
    s = SLOPES[trend]
    j = np.arange(n_h4, dtype=np.float64)
    close = base + s * j + amp * np.where(np.arange(n_h4) % 2 == 0, 1.0, -1.0)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": T0 + pd.to_timedelta(np.arange(n_h4) * 4, unit="h"),
        "open": open_,
        "high": np.maximum(open_, close) + 1.0,
        "low": np.minimum(open_, close) - 1.0,
        "close": close,
    })


def h1_times(count: int = N_H1) -> pd.DatetimeIndex:
    return T0 + pd.to_timedelta(np.arange(count), unit="h")


def _signed(ref: float, offsets: tuple[float, float, float, float], side: str) -> tuple[float, float, float, float]:
    o, h, lo, c = offsets
    if side == "buy":
        return ref + o, ref + h, ref + lo, ref + c
    return ref - o, ref - lo, ref - h, ref - c


@dataclass
class Scenario:
    trend: str
    side: str
    h4: pd.DataFrame
    h1: pd.DataFrame
    lines: ChannelBars
    t: int = DEFAULT_T
    params: dict[str, Any] = field(default_factory=lambda: dict(SCHEMA.defaults()))

    # ---- line access (buy-signed names: "support" = lower for buy / upper for sell)
    def line(self, name: str, i: int) -> float:
        return float(self.lines.line(name)[i])

    def support(self) -> str:
        return "lower" if self.side == "buy" else "upper"

    def far(self) -> str:
        return "upper" if self.side == "buy" else "lower"

    def sign(self) -> float:
        return 1.0 if self.side == "buy" else -1.0

    # ---- bar editing
    def set_bar(self, i: int, o: float, h: float, lo: float, c: float) -> None:
        self.h1.loc[i, ["open", "high", "low", "close"]] = [o, h, lo, c]

    def put(self, i: int, ref: float, offsets: tuple[float, float, float, float]) -> None:
        self.set_bar(i, *_signed(ref, offsets, self.side))

    def put_rel(self, i: int, line: str, shift: float, offsets: tuple[float, float, float, float]) -> None:
        """Candle ``offsets`` around ``line_i + sign * shift``."""
        self.put(i, self.line(line, i) + self.sign() * shift, offsets)

    def background(self, line: str, shift: float, start: int = 0, stop: int | None = None) -> None:
        """Background bars at ``line + sign*shift`` (extrapolated before the channel exists)."""
        stop = len(self.h1) if stop is None else stop
        values = self.lines.line(line)
        first = FIRST_VALID_H1
        per_bar = SLOPES[self.trend] / 4.0
        idx = np.arange(start, stop)
        base = np.where(np.isfinite(values[idx]), values[idx], values[first] - per_bar * (first - idx))
        o, h, lo, c = _signed(base + self.sign() * shift, BG, self.side)  # works element-wise on arrays
        self.h1.loc[start:stop - 1, ["open", "high", "low", "close"]] = np.column_stack([o, h, lo, c])

    # ---- views
    def prefix(self, t: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
        t = self.t if t is None else t
        h1 = self.h1.iloc[: t + 1].reset_index(drop=True)
        decision = self.h1["time"].iloc[t] + timedelta(hours=1)
        return h1, slice_closed_bars(self.h4, H4_DURATION, decision).reset_index(drop=True)

    def ctx(self, t: int | None = None, *, has_open_trade: bool = False, account: AccountSettings | None = None,
            h4: pd.DataFrame | None = None) -> StrategyContext:
        h1, h4_closed = self.prefix(t)
        return StrategyContext(
            symbol=SYMBOL, h1=h1, h4=h4_closed if h4 is None else h4, account=account or AccountSettings(),
            params=self.params, has_open_trade=has_open_trade,
        )

    def bars(self) -> ChannelBars:
        """Indicators on the current (edited) H1 data."""
        p, _ = resolve_params(self.params)
        return compute_channel_bars(self.h1, self.h4, p)


def base_scenario(trend: str, side: str, *, t: int = DEFAULT_T, params: dict[str, Any] | None = None) -> Scenario:
    h4 = make_h4(trend)
    times = h1_times()
    flat_bars = pd.DataFrame({"time": times, "open": 2000.0, "high": 2000.5, "low": 1999.5, "close": 2000.0})
    full = dict(SCHEMA.defaults())
    full.update(params or {})
    p, _ = resolve_params(full)
    lines = compute_channel_bars(flat_bars, h4, p)
    return Scenario(trend=trend, side=side, h4=h4, h1=flat_bars.copy(), lines=lines, t=t, params=full)


# Setup expected for each (kind, side).
EXPECTED_SETUP = {
    ("bounce_outer", "buy"): "bounce_lower",
    ("bounce_outer", "sell"): "bounce_upper",
    ("bounce_mid", "buy"): "bounce_mid",
    ("bounce_mid", "sell"): "bounce_mid",
    ("breakout_outer", "buy"): "breakout_upper_pullback",
    ("breakout_outer", "sell"): "breakout_lower_pullback",
    ("breakout_mid", "buy"): "breakout_mid_pullback",
    ("breakout_mid", "sell"): "breakout_mid_pullback",
}
KINDS = ("bounce_outer", "bounce_mid", "breakout_outer", "breakout_mid")


def allowed(trend: str, side: str) -> bool:
    return trend == "flat" or (trend == "up") == (side == "buy")


def build(
    trend: str,
    side: str,
    kind: str,
    *,
    t: int = DEFAULT_T,
    touch_at: int = 0,
    confirm: bool = True,
    closed_through: bool = False,
    bars_after_breakout: int = 4,
    fail_at: int | None = None,
    dip_within_tol: bool = False,
    params: dict[str, Any] | None = None,
) -> Scenario:
    """One setup arrangement ending with the confirmation bar ``t`` (bars after ``t`` = background).

    kind: ``bounce_outer`` (buy: lower line / sell: upper line), ``bounce_mid`` (buy from above / sell
    from below), ``breakout_outer`` (buy: upper / sell: lower), ``breakout_mid``.
    touch_at: 0 = the confirmation bar touches; k > 0 = only bar t-k touches (t floats 1.0 off the line).
    confirm: False -> bar t touches but is not a reversal pattern.
    closed_through: bounce with touch at t-1 and a hammer at t that closes 0.65 beyond the line.
    bars_after_breakout: t - b for breakouts.
    fail_at: breakouts: bar b+fail_at closes 1.0 back through the line; later bars stay there.
    dip_within_tol: breakouts: bar t-1 closes 0.02 back through the line (inside the tolerance).
    """
    sc = base_scenario(trend, side, t=t, params=params)
    if kind == "bounce_outer":
        line, bg_line, bg_shift = sc.support(), "mid", -5.0
    elif kind == "bounce_mid":
        line, bg_line, bg_shift = "mid", "mid", 5.0
    elif kind == "breakout_outer":
        line, bg_line, bg_shift = sc.far(), "mid", 5.0
    elif kind == "breakout_mid":
        line, bg_line, bg_shift = "mid", "mid", -5.0
    else:
        raise ValueError(kind)
    sc.background(bg_line, bg_shift)

    if kind.startswith("breakout"):
        b = t - bars_after_breakout
        sc.put_rel(b, line, 0.0, BREAKOUT)
        for i in range(b + 1, t):
            sc.put_rel(i, line, 2.0, BG)  # holding beyond the line, no touch
        if fail_at is not None:
            for i in range(b + fail_at, t):
                sc.put_rel(i, line, -2.0, BG)
            sc.put_rel(b + fail_at, line, -1.25, BG)  # close = line - 1.0
        if dip_within_tol:
            sc.put_rel(t - 1, line, -0.27, BG)  # close = line - 0.02 (inside tol >= 0.09), touches

    if closed_through:
        sc.put_rel(t - 1, line, 0.0, PLAIN_TOUCH)
        sc.put_rel(t, line, -3.0, HAMMER)  # close = line - 0.65
    elif touch_at > 0:
        sc.put_rel(t - touch_at, line, 0.0, PLAIN_TOUCH)
        for i in range(t - touch_at + 1, t):
            sc.put_rel(i, line, 1.5, BG)
        sc.put_rel(t, line, 1.0, HAMMER if confirm else PLAIN_TOUCH)
    else:
        sc.put_rel(t, line, 0.0, HAMMER if confirm else PLAIN_TOUCH)
    return sc


def conflict_scenario(t: int = DEFAULT_T) -> Scenario:
    """Flat channel; bar t touches BOTH bands and is a bullish pin bar AND a bearish engulfing."""
    sc = base_scenario("flat", "buy", t=t)
    sc.background("mid", 5.0)
    mid = sc.line("mid", t)
    upper, lower = sc.line("upper", t), sc.line("lower", t)
    sc.set_bar(t - 1, mid + 4.5, mid + 5.6, mid + 4.4, mid + 5.5)  # small bullish bar
    sc.set_bar(t, mid + 6.0, upper + 0.05, lower - 0.05, mid + 4.0)
    return sc


def boundary_scenario(side: str, extra: float, t: int = DEFAULT_T) -> tuple[Scenario, float]:
    """Up (buy) / down (sell) channel, touch of the outer line ONLY by bar t, at distance ``tol_t + extra``.

    Bar t-1 lies entirely beyond the line (gap), so TR_t = |far extreme of t - close_{t-1}| does not
    depend on the touching extreme of bar t: ATR_H1(t) and tol_t stay fixed while the touch moves.
    Returns the scenario and tol_t.
    """
    trend = "up" if side == "buy" else "down"
    sc = base_scenario(trend, side, t=t)
    sc.background("mid", -5.0)
    line = sc.support()
    sc.put_rel(t - 1, line, -3.0, (0.25, 0.45, -0.45, -0.25))  # entirely beyond the line
    L = sc.line(line, t)

    def place(touch_price: float) -> None:
        if side == "buy":
            sc.set_bar(t, L + 2.2, L + 2.8, touch_price, L + 2.6)
        else:
            sc.set_bar(t, L - 2.2, touch_price, L - 2.8, L - 2.6)

    place(L + 0.1 * sc.sign())
    tol = float(sc.bars().tol[t])
    # Same float expression as the touch rule (line + tol / line - tol), then moved by `extra`.
    edge = L + tol if side == "buy" else L - tol
    place(edge + sc.sign() * extra if extra else edge)
    return sc, tol


# ---------------------------------------------------------------------------------------------- random walk
# Moved to the engine (plugin validation uses it at runtime); same numbers, same signature.
from alpha_engine.plugins.fixture import random_walk  # noqa: E402,F401
