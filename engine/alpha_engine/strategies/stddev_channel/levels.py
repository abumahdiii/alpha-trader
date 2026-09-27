"""Entry, stop-loss and take-profit rules (stddev-channel-system.md section 5).

* **Entry** = OPEN of the H1 bar after the confirmation bar ``t`` (bar ``t+1``). It is NOT known at the
  decision time (close of ``t``), so a :class:`SignalCandidate` carries only ``reference_price`` =
  ``close_t`` (indicative) and the real entry is supplied at fill time.
* **Stop loss** (known at decision time, fixed)::

      buy:  SL = low_t  - ATR_H1(t) * sl_atr_mult
      sell: SL = high_t + ATR_H1(t) * sl_atr_mult

* **Take profit** = ``entry +/- rr * |entry - SL|`` with ``rr`` from :class:`AccountSettings`.
  At decision time only the indicative TP from ``reference_price`` exists; the real TP is computed by
  :func:`resolve_trade_levels` (-> ``SignalCandidate.resolve_levels``) once the entry is known.

Worked example (buy, rr = 2, sl_atr_mult = 0.2): confirmation bar low 1998.40, ATR_H1 = 2.50
    SL = 1998.40 - 2.50 * 0.2 = 1997.90
    close_t = 2000.00 -> indicative TP = 2000.00 + 2 * 2.10 = 2004.20
    next bar opens 2001.00 -> entry 2001.00, TP = 2001.00 + 2 * |2001.00 - 1997.90| = 2001.00 + 6.20 = 2007.20
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from ...strategy.signal import Direction, SignalCandidate, take_profit_from


def stop_loss_price(direction: Direction, bar_high: float, bar_low: float, atr_h1: float, sl_atr_mult: float) -> float:
    """SL beyond the confirmation bar's extreme plus the ATR buffer (see module docstring)."""
    buffer = atr_h1 * sl_atr_mult
    if not math.isfinite(buffer) or buffer < 0:
        raise ValueError(f"SL buffer must be a finite number >= 0, got {buffer!r}")
    return bar_low - buffer if direction == "buy" else bar_high + buffer


def take_profit_price(entry: float, stop_loss: float, rr: float, direction: Direction) -> float:
    """``entry +/- rr * |entry - stop_loss|``."""
    return take_profit_from(entry, stop_loss, rr, direction)


def entry_price_for(h1: pd.DataFrame, confirmation_index: int) -> float | None:
    """Open of bar ``confirmation_index + 1`` (the fill price); ``None`` while that bar does not exist.

    Only the backtester / live layer calls this, AFTER bar ``t+1`` has opened. The strategy never does.
    """
    nxt = confirmation_index + 1
    if nxt >= len(h1):
        return None
    return float(h1["open"].iloc[nxt])


@dataclass(frozen=True)
class TradeLevels:
    entry: float
    stop_loss: float
    take_profit: float

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.stop_loss)


def resolve_trade_levels(candidate: SignalCandidate, entry_price: float) -> TradeLevels:
    """Final levels at fill time. Raises ``ValueError`` when the entry gapped through the SL."""
    sl, tp = candidate.resolve_levels(entry_price)
    return TradeLevels(entry=float(entry_price), stop_loss=sl, take_profit=tp)
