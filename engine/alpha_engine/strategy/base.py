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

Engine hooks (strategy contract S1)
-----------------------------------
The backtester, the chart and (later) the plugin host only talk to a strategy through these members, so no
engine module needs to know which strategy it runs:

* ``evaluate(ctx)`` -- the decision on ONE closed bar (abstract);
* ``scan(h1, h4, params, account, *, symbol)`` -- the candidates of EVERY H1 bar of a full history, ignoring
  the open-trade state. Default: :func:`evaluate_checked` on each closed prefix ``h1[:t+1]`` with the H4 bars
  closed at ``open_t + 1h`` -- correct by construction but O(n^2) on long histories; a strategy may override
  it with a vectorised version that returns exactly the same candidates (tests/test_no_lookahead.py,
  tests/test_strategy_contract.py);
* ``first_valid_index(h1, h4, params)`` -- first H1 bar at which a decision is possible (default: from the
  ``history_bars`` class attribute);
* ``warmup_margin_h4_bars(params)`` -- extra H4 bars after that bar before a backtest window may start
  (``backtest.periods.earliest_start``; default 0) and ``warmup_texts_fa`` -- the Persian wording of the
  period errors / notes that mention it;
* ``identity`` -- :class:`StrategyIdentity` (name, code version, source file SHA-256, builtin/plugin), part
  of every cache key and every result's provenance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Literal

import numpy as np
import pandas as pd

from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from .params import ParamSchema, ParamValue, params_hash
from .signal import SignalCandidate, setup_slug

logger = get_logger(__name__)

H1_DURATION = timedelta(hours=1)
H4_DURATION = timedelta(hours=4)
TIME_COLUMN = "time"

StrategySource = Literal["builtin", "plugin"]


class LookAheadError(RuntimeError):
    """A frame handed to a strategy contains a bar that is not closed at decision time."""


class StrategyContractError(RuntimeError):
    """A strategy returned something that violates the ``evaluate`` contract."""


class StrategyParamsError(ValueError):
    """Params rejected by ``Strategy.validate_params`` (Persian messages in ``errors_fa``)."""

    def __init__(self, strategy_name: str, errors_fa: list[str]) -> None:
        super().__init__(f"{strategy_name}: " + "; ".join(errors_fa))
        self.strategy_name = strategy_name
        self.errors_fa = list(errors_fa)


@dataclass(frozen=True)
class StrategyIdentity:
    """Who decided: part of every scan cache key and of every result's provenance.

    ``sha256`` = SHA-256 of the strategy's source file for uploaded plugins (``None`` for built-in code,
    whose identity is the engine build + ``version``); ``source`` = ``builtin`` | ``plugin``.
    """

    name: str
    version: int
    sha256: str | None = None
    source: StrategySource = "builtin"

    @property
    def cache_key(self) -> tuple[str, int, str | None]:
        return (self.name, self.version, self.sha256)

    def label(self) -> str:
        """``name vN [source sha=abcdef012345]`` for logs."""
        sha = "" if self.sha256 is None else f" sha={self.sha256[:12]}"
        return f"{self.name} v{self.version} [{self.source}{sha}]"


@dataclass(frozen=True)
class WarmupTextsFa:
    """Persian wording of the period errors / notes that mention a strategy's warm-up (``{margin}`` = the
    margin in H4 bars). The defaults are strategy-neutral; a strategy overrides ``Strategy.warmup_texts_fa``."""

    no_valid_bar: str = "داده کش برای اجرای سیستم کافی نیست (هیچ کندلی با داده کافی برای تصمیم سیستم وجود ندارد)."
    too_short: str = ("داده کش برای گرم شدن اندیکاتورها کافی نیست: بعد از اولین کندل معتبر سیستم به {margin} کندل "
                      "H4 دیگر (حاشیه گرم شدن اندیکاتورها) نیاز است.")
    window_too_early: str = "سیستم به داده کافی و حاشیه گرم شدن اندیکاتورها ({margin} کندل H4) نیاز دارد."
    limits: str = "سیستم به داده کافی و حاشیه گرم شدن اندیکاتورها ({margin} کندل H4) نیاز دارد"
    clip_reason: str = "گرم شدن اندیکاتورهای سیستم"


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

    Optional class attributes: ``history_bars`` (H1 bars ``evaluate`` needs before it can decide; drives the
    default :meth:`first_valid_index`), ``source`` / ``code_sha256`` (:attr:`identity`; set by the plugin host)
    and ``warmup_texts_fa``. See the module docstring for the engine hooks.
    """

    name: ClassVar[str]
    version: ClassVar[int]
    title_fa: ClassVar[str]
    param_schema: ClassVar[ParamSchema]
    history_bars: ClassVar[int] = 1
    source: ClassVar[StrategySource] = "builtin"
    code_sha256: ClassVar[str | None] = None
    warmup_texts_fa: ClassVar[WarmupTextsFa] = WarmupTextsFa()

    @property
    def identity(self) -> StrategyIdentity:
        return StrategyIdentity(name=self.name, version=self.version, sha256=self.code_sha256, source=self.source)

    def clean_params(self, params: Mapping[str, Any] | None) -> dict[str, ParamValue]:
        """:meth:`validate_params` or :class:`StrategyParamsError`."""
        clean, errors = self.validate_params(params)
        if errors:
            raise StrategyParamsError(self.name, errors)
        return clean

    @classmethod
    def validate_params(cls, values: Mapping[str, Any] | None) -> tuple[dict[str, ParamValue], list[str]]:
        """Schema validation plus the strategy's cross-field rules: ``(clean, errors_fa)``.

        Same contract as :meth:`ParamSchema.validate` (on any error ``clean`` is ``{}``). Default: the
        schema only. Override to add rules the schema cannot express, so the params store / API reject a
        parameter set the strategy would refuse at evaluation time.
        """
        return cls.param_schema.validate(values)

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

    # ------------------------------------------------------------------ engine hooks (defaults)
    def first_valid_index(self, h1: pd.DataFrame, h4: pd.DataFrame, params: Mapping[str, Any] | None) -> int | None:
        """First H1 bar index at which ``evaluate`` can decide (``None``: never in this history).

        Default: bar ``history_bars - 1`` (the first bar with ``history_bars`` closed H1 bars up to it)."""
        need = max(1, int(self.history_bars))
        return need - 1 if len(h1) >= need else None

    def warmup_margin_h4_bars(self, params: Mapping[str, Any] | None) -> int:
        """Extra H4 bars after :meth:`first_valid_index` before a backtest window may start (path-dependent
        indicators such as Wilder ATR still carry their seed). Default: 0."""
        return 0

    def scan(
        self,
        h1: pd.DataFrame,
        h4: pd.DataFrame,
        params: Mapping[str, Any] | None,
        account: AccountSettings,
        *,
        symbol: str,
    ) -> list[SignalCandidate]:
        """Candidates of every H1 bar of ``h1`` (chronological), ignoring the open-trade state.

        Default: :func:`evaluate_checked` on the closed prefix of every bar -- H1 ``h1[:t+1]`` and the H4 bars
        with ``open + 4h <= open_t + 1h`` -- with the validated params (``prepare`` is not used here). Correct
        by construction (the contract is checked per bar) but O(n^2); override with an equivalent vectorised
        version for long histories. ``h1`` must be sorted by open time.
        """
        clean = self.clean_params(params)
        phash = params_hash(clean)
        if len(h1) == 0:
            return []
        h1_ns = bar_open_times(h1).as_unit("ns").asi8
        if len(h1_ns) > 1 and not (np.diff(h1_ns) > 0).all():
            raise ValueError("scan: H1 bars must be sorted by open time without duplicates")
        h4_times = bar_open_times(h4)
        h4_sorted = h4_times.is_monotonic_increasing
        h4_close_ns = h4_times.as_unit("ns").asi8 + pd.Timedelta(H4_DURATION).value
        h1_step = pd.Timedelta(H1_DURATION).value
        out: list[SignalCandidate] = []
        for t in range(len(h1_ns)):
            decision_ns = int(h1_ns[t]) + h1_step
            if h4_sorted:
                h4_t = h4.iloc[: int(np.searchsorted(h4_close_ns, decision_ns, side="right"))]
            else:
                h4_t = slice_closed_bars(h4, H4_DURATION, pd.Timestamp(decision_ns, tz="UTC"))
            ctx = StrategyContext(symbol=symbol, h1=h1.iloc[: t + 1], h4=h4_t, account=account, params=clean)
            cand = evaluate_checked(self, ctx, phash)
            if cand is not None:
                out.append(cand)
        if is_dev_mode():
            logger.debug("%s %s default scan (per-bar evaluate): bars=%d candidates=%d params=%s",
                         self.identity.label(), symbol, len(h1_ns), len(out), phash[:12])
        return out


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
            strategy.name, ctx.symbol, decision_time.isoformat(), result.direction, setup_slug(result.setup),
            result.line, result.reference_price, result.stop_loss, result.extra,
        )
    return result
