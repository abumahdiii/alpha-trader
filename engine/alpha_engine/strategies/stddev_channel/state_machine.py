"""Breakout -> pullback -> confirmation state machine, and the one-candidate decision at bar ``t``.

Stateless by design
-------------------
There is no mutable state carried between calls. The pending-breakout state at bar ``t`` is RE-DERIVED
from the last ``M + 2`` H1 bars (``M`` = ``pullback_window``), so ``decide_at(cb, t)`` is a pure function
of the (causal) arrays at bars ``<= t``. ``evaluate`` on the history up to ``t`` and ``scan`` over the
whole history therefore give identical answers.

Breakout-pullback rule (buy side on line ``L`` = upper or mid; the sell side is the exact mirror)::

    breakout bar b:   close_b > L_b  and  close_{b-1} <= L_{b-1}          (close crosses above the line)
    window:           t - M <= b <= t - 1      (pullback + confirmation within the M bars after b)
    latest wins:      b = the most recent breakout bar in the window
    still pending:    no bar i in [b+1, t] closed below L_i - tol_i        (failed breakout -> cancelled)
                      and the line is defined on every bar in [b, t]
    pullback touch:   some bar i in [max(b+1, t - touch_lookback), t] touches L (the breakout bar itself
                      does not count as the pullback)
    confirmation:     bullish reversal pattern at t, buy allowed by the channel at t,
                      close_t >= L_t - tol_t

Mirror (sell) on ``L`` = lower or mid: close crosses BELOW the line, failure = close above ``L + tol``,
bearish confirmation, ``close_t <= L_t + tol_t``.

Why "latest wins" is exact: if the most recent breakout ``b2`` has been cancelled by a failed close in
``[b2+1, t]``, every older breakout ``b1 < b2`` is cancelled too (its range ``[b1+1, t]`` contains that
close). An "opposite breakout" of the same line (a close beyond the line on the other side by more than
``tol``) is exactly the failure condition; a close within ``tol`` on the other side is treated as part of
the pullback, consistent with the tolerance used for the confirmation close.

Window counting is in actual H1 bars (rows), so a weekend gap does not consume the window.

Direction filter: the channel direction at the DECISION bar ``t`` decides (see ``setups``).

Priority (one candidate per bar), highest first::

    breakout_upper_pullback, breakout_lower_pullback, breakout_mid_pullback,
    bounce_lower, bounce_upper, bounce_mid

(breakout pullbacks before bounces, then the outer line before the mid line). If setups of BOTH
directions fire on the same bar (only possible in a flat channel, e.g. a bar that is both a bullish pin
bar and a bearish engulfing while touching both bands), the signal is ambiguous and NO candidate is
produced (conservative).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ...strategy.signal import Setup
from .params import StdDevParams
from .setups import ChannelBars, LineName, SetupHit, Side, bounce_hits, closed_on_side, confirmed, touch_offset

PRIORITY: tuple[Setup, ...] = (
    Setup.BREAKOUT_UPPER_PULLBACK,
    Setup.BREAKOUT_LOWER_PULLBACK,
    Setup.BREAKOUT_MID_PULLBACK,
    Setup.BOUNCE_LOWER,
    Setup.BOUNCE_UPPER,
    Setup.BOUNCE_MID,
)
_RANK = {setup: rank for rank, setup in enumerate(PRIORITY)}

# (setup, side, line) of every breakout-pullback variant.
BREAKOUT_VARIANTS: tuple[tuple[Setup, Side, LineName], ...] = (
    (Setup.BREAKOUT_UPPER_PULLBACK, "buy", "upper"),
    (Setup.BREAKOUT_MID_PULLBACK, "buy", "mid"),
    (Setup.BREAKOUT_LOWER_PULLBACK, "sell", "lower"),
    (Setup.BREAKOUT_MID_PULLBACK, "sell", "mid"),
)


def is_breakout(cb: ChannelBars, line: str, b: int, side: Side) -> bool:
    """Close of bar ``b`` crosses the line in the ``side`` direction (``b >= 1``)."""
    if b < 1:
        return False
    values = cb.line(line)
    if side == "buy":
        return bool(cb.close[b] > values[b] and cb.close[b - 1] <= values[b - 1])
    return bool(cb.close[b] < values[b] and cb.close[b - 1] >= values[b - 1])


def failed_at(cb: ChannelBars, line: str, i: int, side: Side) -> bool:
    """Bar ``i`` cancels a pending breakout: it closed back through the line by more than ``tol``,
    or the line/tolerance is undefined there (conservative)."""
    value = cb.line(line)[i]
    tol = cb.tol[i]
    if not (np.isfinite(value) and np.isfinite(tol)):
        return True
    if side == "buy":
        return bool(cb.close[i] < value - tol)
    return bool(cb.close[i] > value + tol)


def pending_breakout(cb: ChannelBars, line: str, t: int, side: Side, window: int) -> int | None:
    """Index ``b`` of the breakout still pending at bar ``t`` (before its confirmation), else ``None``.

    Reads bars ``t - window - 1 .. t`` only.
    """
    b = None
    for cand in range(t - 1, max(t - window, 1) - 1, -1):  # most recent first
        if is_breakout(cb, line, cand, side):
            b = cand
            break
    if b is None:
        return None
    for i in range(b + 1, t + 1):
        if failed_at(cb, line, i, side):
            return None
    return b


def breakout_hits(cb: ChannelBars, t: int, p: StdDevParams) -> list[SetupHit]:
    hits: list[SetupHit] = []
    for setup, side, line in BREAKOUT_VARIANTS:
        if not confirmed(cb, t, side):
            continue
        b = pending_breakout(cb, line, t, side, p.pullback_window)
        if b is None:
            continue
        k = touch_offset(cb, line, t, p.touch_lookback, first_allowed=b + 1)
        if k is None or not closed_on_side(cb, line, t, side):
            continue
        hits.append(SetupHit(setup, side, line, k, breakout_bar=b))
    return hits


@dataclass(frozen=True)
class Decision:
    """Outcome at bar ``t``: all setups that fired, and the chosen one (``None`` = no suggestion)."""

    t: int
    hits: tuple[SetupHit, ...]
    chosen: SetupHit | None
    note: str  # "no_setup" | "chosen" | "direction_conflict"

    @property
    def others(self) -> tuple[SetupHit, ...]:
        return tuple(h for h in self.hits if h is not self.chosen)


def decide_at(cb: ChannelBars, t: int, p: StdDevParams) -> Decision:
    """All setups at bar ``t`` and the single candidate chosen by :data:`PRIORITY`."""
    if not (cb.buy_ok[t] and cb.bull[t]) and not (cb.sell_ok[t] and cb.bear[t]):
        return Decision(t, (), None, "no_setup")  # every setup needs a confirmation at t
    hits = breakout_hits(cb, t, p) + bounce_hits(cb, t, p)
    if not hits:
        return Decision(t, (), None, "no_setup")
    hits.sort(key=lambda h: (_RANK[h.setup], h.side))
    if len({h.side for h in hits}) > 1:
        return Decision(t, tuple(hits), None, "direction_conflict")
    return Decision(t, tuple(hits), hits[0], "chosen")
