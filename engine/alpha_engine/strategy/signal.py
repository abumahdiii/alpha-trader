"""``SignalCandidate``: what a strategy decides on a closed H1 bar.

Look-ahead guard (``.claude/rules/03_trading_safety.md`` section 3)
-------------------------------------------------------------------
A decision is taken when the confirmation H1 bar *t* has CLOSED (``decision_time_utc`` = close time of
bar *t* = its open + 1h). The fill happens at the OPEN of bar *t+1* (``entry_rule = "next_bar_open"``),
a price the strategy cannot know at decision time. Therefore:

* a candidate has **no entry field**. ``reference_price`` is the close of bar *t*, indicative only;
* ``stop_loss`` is known at decision time (low/high of bar *t* -/+ ATR buffer) and is fixed;
* ``indicative_take_profit`` is computed from ``reference_price`` for display only;
* the real TP is computed at fill time with :meth:`SignalCandidate.resolve_levels(entry_price)`,
  where ``entry_price`` is the open of bar *t+1* supplied by the backtester / live signal layer:
  ``tp = entry +/- rr * |entry - sl|``.

Worked example (buy, rr = 2)::

    reference_price = 2000.00, stop_loss = 1995.00
    indicative_take_profit = 2000.00 + 2 * |2000.00 - 1995.00| = 2010.00
    next bar opens at 2001.00 -> resolve_levels(2001.00) = (1995.00, 2001.00 + 2 * 6.00) = (1995.00, 2013.00)

Prices are not rounded here (no symbol spec in this layer); rounding to the symbol's ``digits`` is the
caller's job.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

Direction = Literal["buy", "sell"]
Line = Literal["lower", "mid", "upper"]
EntryRule = Literal["next_bar_open"]

H1 = timedelta(hours=1)
_HASH_RE = r"^[0-9a-f]{64}$"
# Relative tolerance when a caller passes indicative_take_profit explicitly (e.g. a JSON round-trip).
_TP_REL_TOL = 1e-9


class Setup(StrEnum):
    BOUNCE_LOWER = "bounce_lower"
    BOUNCE_MID = "bounce_mid"
    BOUNCE_UPPER = "bounce_upper"
    BREAKOUT_UPPER_PULLBACK = "breakout_upper_pullback"
    BREAKOUT_MID_PULLBACK = "breakout_mid_pullback"
    BREAKOUT_LOWER_PULLBACK = "breakout_lower_pullback"


# Channel line each setup trades at.
SETUP_LINE: dict[Setup, Line] = {
    Setup.BOUNCE_LOWER: "lower",
    Setup.BOUNCE_MID: "mid",
    Setup.BOUNCE_UPPER: "upper",
    Setup.BREAKOUT_UPPER_PULLBACK: "upper",
    Setup.BREAKOUT_MID_PULLBACK: "mid",
    Setup.BREAKOUT_LOWER_PULLBACK: "lower",
}

# Direction forced by the setup itself (stddev-channel-system.md section 4): a bounce off the lower line
# or a pullback after breaking the upper line can only be a buy, and mirrored for sells. Mid-line setups
# go either way (direction comes from the slope, or both in a flat channel).
SETUP_DIRECTION: dict[Setup, Direction | None] = {
    Setup.BOUNCE_LOWER: "buy",
    Setup.BOUNCE_MID: None,
    Setup.BOUNCE_UPPER: "sell",
    Setup.BREAKOUT_UPPER_PULLBACK: "buy",
    Setup.BREAKOUT_MID_PULLBACK: None,
    Setup.BREAKOUT_LOWER_PULLBACK: "sell",
}

ExtraValue = float | int | bool | str | None


def take_profit_from(price: float, stop_loss: float, rr: float, direction: Direction) -> float:
    """``price +/- rr * |price - stop_loss|`` (``+`` for buy, ``-`` for sell)."""
    distance = abs(price - stop_loss)
    return price + rr * distance if direction == "buy" else price - rr * distance


def _require_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be in UTC (offset 0), got offset {value.utcoffset()}")
    return value.astimezone(timezone.utc)


class SignalCandidate(BaseModel):
    """A strategy's decision on a closed H1 bar. Immutable. Carries no entry price (see module doc)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    strategy_name: str = Field(min_length=1)
    strategy_version: int = Field(ge=1)  # code version of the strategy class
    params_hash: str = Field(pattern=_HASH_RE)
    symbol: str = Field(min_length=1)
    direction: Direction
    setup: Setup
    line: Line
    decision_time_utc: datetime  # CLOSE time of the confirmation H1 bar
    confirmation_bar_open_utc: datetime
    entry_rule: EntryRule = "next_bar_open"
    reference_price: float = Field(gt=0)  # close of the confirmation bar; indicative only
    stop_loss: float = Field(gt=0)
    rr: float = Field(gt=0)
    # Derived from reference_price; if passed explicitly it must match (JSON round-trips).
    indicative_take_profit: float | None = None
    pattern: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    reason_fa: str = Field(min_length=1)
    extra: dict[str, ExtraValue] = Field(default_factory=dict)

    @field_validator("decision_time_utc", "confirmation_bar_open_utc")
    @classmethod
    def _utc_only(cls, value: datetime, info: ValidationInfo) -> datetime:
        return _require_utc(value, info.field_name)

    @field_validator("extra")
    @classmethod
    def _finite_extra(cls, value: dict[str, ExtraValue]) -> dict[str, ExtraValue]:
        for key, item in value.items():
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"extra[{key!r}] must be finite")
        return value

    @model_validator(mode="after")
    def _check(self) -> SignalCandidate:
        if self.decision_time_utc - self.confirmation_bar_open_utc != H1:
            raise ValueError(
                "decision_time_utc must be the close of the confirmation H1 bar "
                "(confirmation_bar_open_utc + 1h)"
            )
        if SETUP_LINE[self.setup] != self.line:
            raise ValueError(f"setup {self.setup.value!r} trades the {SETUP_LINE[self.setup]!r} line, not {self.line!r}")
        forced = SETUP_DIRECTION[self.setup]
        if forced is not None and forced != self.direction:
            raise ValueError(f"setup {self.setup.value!r} can only be a {forced!r}")
        if self.direction == "buy" and not self.stop_loss < self.reference_price:
            raise ValueError("buy: stop_loss must be below reference_price")
        if self.direction == "sell" and not self.stop_loss > self.reference_price:
            raise ValueError("sell: stop_loss must be above reference_price")

        expected = take_profit_from(self.reference_price, self.stop_loss, self.rr, self.direction)
        if not math.isfinite(expected) or expected <= 0:
            raise ValueError("indicative take-profit is not a positive finite price (rr too large for a sell?)")
        if self.indicative_take_profit is None:
            # Frozen model: set the derived value once, during validation.
            object.__setattr__(self, "indicative_take_profit", expected)
        elif not math.isclose(self.indicative_take_profit, expected, rel_tol=_TP_REL_TOL, abs_tol=0.0):
            raise ValueError(
                f"indicative_take_profit {self.indicative_take_profit} != reference_price +/- rr*|ref - sl| = {expected}"
            )
        return self

    @property
    def fill_bar_open_utc(self) -> datetime:
        """Open time of bar t+1, where the entry is filled (== ``decision_time_utc``)."""
        return self.decision_time_utc

    def resolve_levels(self, entry_price: float) -> tuple[float, float]:
        """Return ``(stop_loss, take_profit)`` for the actual fill price (open of bar t+1).

        Call this only at fill time. ``tp = entry +/- rr * |entry - sl|``. Raises ``ValueError`` if the
        entry is not a positive finite price or is on the wrong side of (or equal to) the stop loss --
        e.g. the next bar gapped through the stop -- so the caller must reject the setup explicitly
        instead of opening a trade with a negative risk.
        """
        if isinstance(entry_price, bool) or not isinstance(entry_price, (int, float)):
            raise ValueError("entry_price must be a number")
        entry = float(entry_price)
        if not math.isfinite(entry) or entry <= 0:
            raise ValueError("entry_price must be a positive finite price")
        if self.direction == "buy" and not entry > self.stop_loss:
            raise ValueError(f"buy entry {entry} is not above stop_loss {self.stop_loss} (gap through the stop)")
        if self.direction == "sell" and not entry < self.stop_loss:
            raise ValueError(f"sell entry {entry} is not below stop_loss {self.stop_loss} (gap through the stop)")
        tp = take_profit_from(entry, self.stop_loss, self.rr, self.direction)
        if not math.isfinite(tp) or tp <= 0:
            raise ValueError("resolved take-profit is not a positive finite price")
        return self.stop_loss, tp
