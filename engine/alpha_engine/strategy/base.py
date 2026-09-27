"""Strategy interface and the no-look-ahead contract.

Bar frames
----------
H1/H4 frames are pandas DataFrames of OHLC bars. The bar OPEN time (tz-aware UTC) is taken from a
``time`` column if present, otherwise from a ``DatetimeIndex``. A bar with open ``T`` on timeframe
``d`` is CLOSED at ``T + d``.

Decision time
-------------
The strategy decides on the closed H1 bar *t*: ``decision_time = open(t) + 1h``. At that moment it may
only see

* H1 bars with ``open + 1h <= decision_time`` (i.e. up to and including *t*), and
* H4 bars with ``open + 4h <= decision_time`` (H4 bars closed by *t*'s close).

The caller builds these frames (:func:`slice_closed_bars`); a strategy never slices future data out
of a bigger frame itself. :func:`assert_closed_bars` checks the rule and :func:`evaluate_checked`
enforces the whole contract around ``Strategy.evaluate``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pandas as pd

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from .params import ParamSchema
from .signal import SignalCandidate

logger = get_logger(__name__)

H1_DURATION = timedelta(hours=1)
H4_DURATION = timedelta(hours=4)
TIME_COLUMN = "time"


class LookAheadError(RuntimeError):
    """A frame handed to a strategy contains a bar that is not closed at decision time."""


class StrategyContractError(RuntimeError):
    """A strategy returned something that violates the ``evaluate`` contract."""


def bar_open_times(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """UTC bar open times from the ``time`` column or the index. Raises on naive / non-UTC times."""
    if TIME_COLUMN in frame.columns:
        times = pd.DatetimeIndex(frame[TIME_COLUMN])
    elif isinstance(frame.index, pd.DatetimeIndex):
        times = frame.index
    else:
        raise ValueError("bar frame needs a 'time' column or a DatetimeIndex of bar open times")
    if times.tz is None:
        # Naive times are ambiguous (broker server time?); UTC conversion belongs to the data adapter.
        raise ValueError("bar open times must be timezone-aware UTC (naive timestamps found)")
    # tz-aware instants convert exactly; comparisons below are done in UTC.
    return times.tz_convert(timezone.utc)


def _as_utc(value: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError("decision_time must be timezone-aware UTC")
    return ts.tz_convert(timezone.utc)


def decision_time_for(h1: pd.DataFrame) -> pd.Timestamp:
    """Close time of the last H1 bar in ``h1`` (the moment the strategy decides)."""
    times = bar_open_times(h1)
    if len(times) == 0:
        raise ValueError("empty H1 frame has no decision time")
    return times.max() + H1_DURATION


def slice_closed_bars(frame: pd.DataFrame, bar_duration: timedelta, decision_time: datetime) -> pd.DataFrame:
    """Rows of ``frame`` whose bar is closed at ``decision_time`` (``open + duration <= decision_time``)."""
    cutoff = _as_utc(decision_time)
    times = bar_open_times(frame)
    mask = (times + bar_duration) <= cutoff
    return frame.loc[mask]


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may look at when deciding on the closed H1 bar *t*.

    ``indicators`` holds optional precomputed, causal indicator frames (from ``Strategy.prepare``)
    already restricted to the same cutoff. ``symbol_spec`` is the phase-1 symbol model (``Any`` until
    it lands). ``has_open_trade`` implements "max one open trade per symbol".
    """

    symbol: str
    h1: pd.DataFrame
    h4: pd.DataFrame
    account: AccountSettings
    params: Mapping[str, Any]
    has_open_trade: bool = False
    indicators: Mapping[str, Any] = field(default_factory=dict)
    symbol_spec: Any = None

    @property
    def decision_time_utc(self) -> pd.Timestamp:
        return decision_time_for(self.h1)


def assert_closed_bars(ctx: StrategyContext, decision_time: datetime) -> None:
    """Raise :class:`LookAheadError` if any H1 bar's open+1h or H4 bar's open+4h is after ``decision_time``."""
    cutoff = _as_utc(decision_time)
    for label, frame, duration in (("H1", ctx.h1, H1_DURATION), ("H4", ctx.h4, H4_DURATION)):
        times = bar_open_times(frame)
        if len(times) == 0:
            continue
        last_close = times.max() + duration
        if last_close > cutoff:
            raise LookAheadError(
                f"{label} bar opened at {times.max().isoformat()} closes at {last_close.isoformat()}, "
                f"after the decision time {cutoff.isoformat()}"
            )


class Strategy(ABC):
    """Base class for a code-defined trading system.

    Class attributes (checked by the registry): ``name`` (identifier, stable), ``version`` (int >= 1;
    bump whenever the decision logic changes), ``title_fa`` (Persian title), ``param_schema``.

    ``evaluate`` contract:

    * use ONLY data in ``ctx`` (never global state, clocks, files or the network);
    * be deterministic: the same ``ctx`` yields the same result;
    * return ``None`` when ``ctx.has_open_trade`` is True (max one open trade), or when there is no setup;
    * a returned :class:`SignalCandidate` has ``decision_time_utc == ctx.decision_time_utc``, the
      strategy's ``name``/``version``, and no entry price (entry = open of the next bar, resolved later).

    Callers should go through :func:`evaluate_checked`, which enforces these points.
    """

    name: ClassVar[str]
    version: ClassVar[int]
    title_fa: ClassVar[str]
    param_schema: ClassVar[ParamSchema]

    def prepare(self, h1_full: pd.DataFrame, h4_full: pd.DataFrame, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Optional vectorised precompute over the full history (e.g. rolling channels).

        Must be CAUSAL: the value at bar *i* may depend only on bars ``<= i`` (rolling windows, no
        centred windows, no ``shift(-k)``, no full-sample normalisation). The backtester slices the
        result to each decision time before building the context. Default: nothing.
        """
        return {}

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> SignalCandidate | None:
        """Decide on the closed H1 bar at the end of ``ctx.h1``. See the class docstring."""


def evaluate_checked(strategy: Strategy, ctx: StrategyContext, params_hash: str | None = None) -> SignalCandidate | None:
    """Run ``strategy.evaluate(ctx)`` with the contract enforced.

    * ``has_open_trade`` -> ``None`` without calling the strategy;
    * frames must contain only closed bars (:func:`assert_closed_bars`);
    * the result must be ``None`` or a ``SignalCandidate`` for this strategy/version, this symbol,
      the context's decision time, and (if given) ``params_hash``.
    """
    if ctx.has_open_trade:
        if is_dev_mode():
            logger.debug("%s %s: open trade exists, no evaluation", strategy.name, ctx.symbol)
        return None
    decision_time = ctx.decision_time_utc
    assert_closed_bars(ctx, decision_time)
    result = strategy.evaluate(ctx)
    if result is None:
        if is_dev_mode():
            logger.debug("%s %s @ %s: no setup", strategy.name, ctx.symbol, decision_time.isoformat())
        return None
    if not isinstance(result, SignalCandidate):
        raise StrategyContractError(f"{strategy.name}.evaluate returned {type(result).__name__}, not SignalCandidate")
    problems = []
    if result.strategy_name != strategy.name or result.strategy_version != strategy.version:
        problems.append("strategy name/version mismatch")
    if result.symbol != ctx.symbol:
        problems.append("symbol mismatch")
    if pd.Timestamp(result.decision_time_utc) != decision_time:
        problems.append(f"decision_time_utc {result.decision_time_utc} != context decision time {decision_time}")
    if params_hash is not None and result.params_hash != params_hash:
        problems.append("params_hash mismatch")
    if problems:
        raise StrategyContractError(f"{strategy.name}.evaluate: " + "; ".join(problems))
    if is_dev_mode():
        logger.debug(
            "%s %s @ %s: %s %s line=%s ref=%s sl=%s extra=%s",
            strategy.name, ctx.symbol, decision_time.isoformat(), result.direction, result.setup.value,
            result.line, result.reference_price, result.stop_loss, result.extra,
        )
    return result
