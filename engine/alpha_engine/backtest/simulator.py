"""Bar-by-bar trade simulation over the H1 bars of ONE window (phase-4 decisions 1-5).

Decisions come from a *candidate provider* ``candidate_at(i, has_open_trade)``: in production the
precomputed full-history ``Strategy.scan`` of the run's strategy (``history.scan_full_history``) indexed by the
confirmation bar; in the equivalence test a per-bar ``evaluate`` on ``h1[:i+1]`` + closed H4 bars. Both give
the same trades (tests/test_backtest_equivalence.py).

Rules (bars are BID prices; ``spr_i`` = spread of bar i in price = points * point, zero spread filled
causally, or with the run's fallback spread before the first broker spread, see ``costs.py``;
``ask = bid + spr``)
------------------------------------------------------------------------------------------------------
Per in-window bar ``i``, in this order:

1. **Entry** of the candidate accepted at the close of bar ``t = i - 1``, at the OPEN of bar ``i``:

   * buy fills at the ask open ``open_i + spr_i``; sell fills at the bid open ``open_i``;
   * **gap through the stop** (skipped, ``gap_through_stop``): the bar opens at/through the SL on the side
     the stop would be executed on -- buy: bid open ``open_i <= SL`` (this includes ask open <= SL);
     sell: ask open ``open_i + spr_i >= SL``. Such a trade would be stopped at once;
   * levels ``candidate.resolve_levels(fill)`` (TP = fill +/- rr * |fill - SL|); a ValueError
     (e.g. TP <= 0) -> skipped ``invalid_levels``;
   * volume ``risk.sizing.size_position`` with the MOMENTARY realized balance, ``risk_pct`` and
     ``leverage`` of the run and the symbol spec (floor to ``volume_step``); rejected -> skipped
     ``sizing_rejected`` with the sizing's Persian reason.

2. **Exit** of the open trade (from the entry bar itself on)::

       long  (bid):  open_i <= SL -> sl_gap @ open_i      | open_i >= TP -> tp_gap @ open_i
                     else low_i <= SL -> sl @ SL          | else high_i >= TP -> tp @ TP
       short (ask):  a = open_i + spr_i
                     a >= SL -> sl_gap @ a                | a <= TP -> tp_gap @ a
                     else high_i + spr_i >= SL -> sl @ SL | else low_i + spr_i <= TP -> tp @ TP

   SL is checked before TP, so a bar touching both counts as a loss (conservative). SL/TP are NOT rounded
   to the symbol's digits (fills are exactly at the computed levels).

   ``pnl = (exit - entry) * dir * volume * value_per_price_unit``, ``value_per_price_unit =
   tick_value / tick_size`` (same as sizing), ``commission = per_lot_per_side * volume * 2``,
   ``net = pnl - commission``, ``R = net / risk_amount`` with ``risk_amount = volume * |entry - SL| *
   value_per_price_unit``. The realized balance changes only at exits. A balance <= 0 ends the window.

3. **Mark to market** at the bar close: ``equity = balance + unrealized - commission`` where the unrealized
   PnL values a long at the bid close and a short at the ask close (``close + spr``) -- the liquidation
   value, so the equity point of the exit bar equals the new balance.

4. **Decision** at the close of bar ``i`` (after its exits): a candidate at ``i`` is accepted only when no
   trade is open; otherwise it is dropped (skipped ``position_open``, never queued). An accepted candidate
   is also skipped when bar ``i+1`` is outside the window or the data (``entry_outside_window``), or when
   the step ``i -> i+1`` is a ``missing`` gap (``missing_gap``). After a weekend the entry bar is simply the
   next cached bar (Sunday's open).

At the end of the window an open trade is closed at the close of the last in-window bar (long at the bid
close, short at the ask close ``close + spr``) with ``end_of_period`` («بسته‌شده در پایان بازه»).

Swap is not modeled; trades held over a weekend (a Saturday UTC between entry and exit) are flagged and
counted (``weekend_holds``).

Worked example (long, gold: point 0.01, tick 0.01 / 1.00 -> 100 per price unit, step 0.01; balance
10000, risk 1 % = 100, rr 2, commission 3.50 per lot per side). Candidate SL 1995.00; entry bar opens
(bid) 2000.00 with spread 25 points -> fill 2000.25; TP = 2000.25 + 2 * 5.25 = 2010.75; raw volume
100 / (5.25 * 100) = 0.190476 -> 0.19 lot; risk_amount = 0.19 * 5.25 * 100 = 99.75. A later bar has high
2011.00 >= TP -> exit 2010.75, gross = 10.50 * 0.19 * 100 = 199.50, commission = 3.50 * 0.19 * 2 = 1.33,
net = 198.17, R = 198.17 / 99.75 = 1.98667, balance 10198.17 (tests/test_backtest_simulator.py).

Shared trade rules (phase 5)
----------------------------
The per-trade rules above live in module-level, strategy-agnostic helpers that take a
:class:`~alpha_engine.strategy.signal.SignalCandidate` plus the bar lists and have no side effects on the
caller's state: :func:`entry_block` (decision-time checks: entry bar in the window/data, ``missing`` gap),
:func:`open_position` (fill at ask/bid, gap through the stop, levels, sizing on the fill price, commission;
returns an :class:`OpenPosition` or an :class:`EntryRejected`), :func:`check_exit` / :func:`exit_hit`
(SL first, gap variants), :func:`exit_time_ns`, :func:`liquidation_price` (end-of-period / mark price) and
:func:`settle` (the :class:`~alpha_engine.backtest.models.Trade` with pnl, commission, R, flags).
:func:`simulate_window` (one open trade at a time, momentary balance) and
``setup_outcomes.simulate_setup`` (each setup on its own, settings balance) both call exactly these, so
the chart's per-setup results and the backtest can never apply different rules
(tests/test_backtest_golden.py pins the simulator's behaviour).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd

from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..risk.sizing import SizingResult, size_position
from ..storage.account_settings import AccountSettings
from ..strategy.signal import SignalCandidate
from .costs import SpreadSeries, commission
from .models import (
    EXIT_REASON_FA,
    SKIP_REASON_FA,
    CostModel,
    EquityPoint,
    ExitReason,
    SkippedCandidate,
    SkipReason,
    Trade,
    Window,
    WindowResult,
)

logger = get_logger(__name__)

H1_NS = 3_600_000_000_000
DAY_NS = 86_400_000_000_000
PROGRESS_EVERY = 500  # bars between progress callbacks / cancel checks

CandidateProvider = Callable[[int, bool], SignalCandidate | None]


class BacktestCancelled(RuntimeError):
    """The run's cancel event was set; no result is produced."""


@dataclass(frozen=True, eq=False)
class BarArrays:
    """Full-history H1 arrays the simulator walks (built once per run by ``history.build_bar_arrays``)."""

    times_ns: np.ndarray  # int64 UTC bar open
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    spread: SpreadSeries
    missing_gap_after: np.ndarray  # bool: the step from bar i to bar i+1 is a "missing" gap
    _lists: dict = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return len(self.times_ns)

    def lists(self) -> dict[str, list]:
        """Python-list views (scalar access in the bar loop is ~10x faster than numpy indexing)."""
        if not self._lists:
            self._lists.update(
                open=self.open.tolist(), high=self.high.tolist(), low=self.low.tolist(), close=self.close.tolist(),
                spr=self.spread.price.tolist(), spr_pts=self.spread.points.tolist(),
                filled=self.spread.filled.tolist(), unfilled=self.spread.unfilled.tolist(),
                fallback=self.spread.fallback.tolist(),
                missing=self.missing_gap_after.tolist(), times=self.times_ns.tolist(),
            )
        return self._lists


def to_ns(value: datetime | pd.Timestamp) -> int:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError("naive datetime: UTC required")
    return int(ts.tz_convert("UTC").as_unit("ns").value)


def ns_to_dt(ns: int) -> datetime:
    return pd.Timestamp(int(ns), tz="UTC").to_pydatetime()


def window_bar_range(times_ns: np.ndarray, window: Window) -> tuple[int, int]:
    """``[i0, i1)``: indices of the bars with ``start <= open < end``."""
    i0 = int(np.searchsorted(times_ns, to_ns(window.start), side="left"))
    i1 = int(np.searchsorted(times_ns, to_ns(window.end), side="left"))
    return i0, max(i0, i1)


def held_over_weekend(entry_ns: int, exit_ns: int) -> bool:
    """A Saturday (UTC) lies within ``[entry, exit)``."""
    first = entry_ns // DAY_NS
    last = max(exit_ns - 1, entry_ns) // DAY_NS
    return any((day + 3) % 7 == 5 for day in range(first, min(last, first + 7) + 1))  # Mon=0; 1970-01-01 = Thu


def scan_provider(by_bar: dict[int, SignalCandidate]) -> CandidateProvider:
    """Provider over precomputed scan candidates (ignores the open-trade flag; the simulator drops them)."""
    get = by_bar.get

    def candidate_at(i: int, has_open_trade: bool) -> SignalCandidate | None:
        return get(i)

    return candidate_at


# ---------------------------------------------------------------------------------------------- shared trade rules
SpreadSource = Literal["historical", "filled", "fallback", "zero"]
BarLists = dict[str, list]  # BarArrays.lists()


@dataclass
class OpenPosition:
    """A filled trade (``open_position``). ``flags`` grows while the trade is walked (``check_exit``)."""

    cand: SignalCandidate
    t: int  # confirmation bar
    entry_index: int  # bar t+1 (the next cached bar)
    entry: float  # fill: buy = ask open, sell = bid open
    entry_bid_open: float
    sl: float
    tp: float
    volume: float
    risk_amount: float  # sizing.actual_risk
    balance_before: float  # balance the volume was sized with
    sign: float
    commission: float  # round trip
    spread_entry_pts: int
    flags: list[str]
    warnings: list[str]
    sizing: SizingResult | None = None
    spread_source: SpreadSource = "historical"


@dataclass(frozen=True)
class EntryRejected:
    """Why a candidate was not entered (a ``SkippedCandidate`` in the backtest, ``rejected`` / ``pending_entry``
    on the chart). The optional fields carry what was known when the entry failed (for display)."""

    reason: SkipReason
    detail: str | None
    reason_fa: str
    entry_index: int | None = None  # bar t+1 when it exists
    bid_open: float | None = None
    fill: float | None = None  # the would-be fill (gap / levels / sizing rejections)
    spread_points: int | None = None
    spread_source: SpreadSource | None = None
    stop_loss: float | None = None
    take_profit: float | None = None  # levels were valid (sizing rejections only)
    sizing: SizingResult | None = None


def value_per_price_unit(spec: SymbolSpec) -> float:
    """Money per 1.0 price move per lot: ``tick_value / tick_size`` (same as the sizing)."""
    return spec.trade_tick_value / spec.trade_tick_size if spec.trade_tick_size else 0.0


def spread_source(L: BarLists, i: int) -> SpreadSource:
    """Where the spread of bar ``i`` came from (see ``costs.py``): the broker's own value, the causal fill,
    the fallback, or zero (no earlier spread and fallback 0)."""
    if L["filled"][i]:
        return "filled"
    if L["fallback"][i]:
        return "fallback"
    if L["unfilled"][i]:
        return "zero"
    return "historical"


def entry_block(L: BarLists, t: int, *, stop: int, window_end: datetime | None = None) -> EntryRejected | None:
    """Decision-time checks for a candidate accepted at the close of bar ``t`` (no open trade): the entry bar
    ``t+1`` must exist in the data and lie before ``stop`` (exclusive bar index of the window end), and the
    step ``t -> t+1`` must not be a ``missing`` gap. ``None`` = the entry may be attempted at ``t+1``."""
    times = L["times"]
    n_total = len(times)
    if t + 1 >= stop or t + 1 >= n_total:
        return EntryRejected(
            SkipReason.ENTRY_OUTSIDE_WINDOW,
            "no bar after the confirmation bar in the data" if t + 1 >= n_total else
            f"entry bar {ns_to_dt(times[t + 1]).isoformat()} >= window end "
            f"{window_end.isoformat() if window_end is not None else '?'}",
            SKIP_REASON_FA[SkipReason.ENTRY_OUTSIDE_WINDOW])
    if L["missing"][t]:
        return EntryRejected(SkipReason.MISSING_GAP,
                             f"{ns_to_dt(times[t]).isoformat()} -> {ns_to_dt(times[t + 1]).isoformat()}",
                             SKIP_REASON_FA[SkipReason.MISSING_GAP], entry_index=t + 1, bid_open=L["open"][t + 1])
    return None


def open_position(
    cand: SignalCandidate,
    t: int,
    i: int,
    L: BarLists,
    *,
    balance: float,
    spec: SymbolSpec,
    account: AccountSettings,
    commission_per_lot_per_side: float,
) -> OpenPosition | EntryRejected:
    """Fill ``cand`` at the open of bar ``i`` (= ``t+1``) with ``balance`` for the sizing (module docstring,
    rule 1): buy at the ask open, sell at the bid open; gap through the stop (buy: bid open <= SL, sell: ask
    open >= SL), invalid levels and sizing rejections give an :class:`EntryRejected`."""
    op = L["open"][i]
    spr = L["spr"][i]
    pts = L["spr_pts"][i]
    ask_open = op + spr
    buy = cand.direction == "buy"
    sl = cand.stop_loss
    fill = ask_open if buy else op
    src = spread_source(L, i)
    known = dict(entry_index=i, bid_open=op, fill=fill, spread_points=pts, spread_source=src, stop_loss=sl)
    if (op <= sl) if buy else (ask_open >= sl):
        side = f"bid open {op!r} <= SL {sl!r}" if buy else f"ask open {ask_open!r} >= SL {sl!r}"
        return EntryRejected(SkipReason.GAP_THROUGH_STOP, f"{side} (spread {pts} points)",
                             SKIP_REASON_FA[SkipReason.GAP_THROUGH_STOP], **known)
    try:
        sl, tp = cand.resolve_levels(fill)
    except ValueError as exc:
        return EntryRejected(SkipReason.INVALID_LEVELS, f"fill {fill!r}: {exc}",
                             SKIP_REASON_FA[SkipReason.INVALID_LEVELS], **known)
    sizing = size_position(
        balance=balance, risk_pct=account.risk_pct, entry=fill, stop_loss=sl,
        tick_value=spec.trade_tick_value, tick_size=spec.trade_tick_size, volume_min=spec.volume_min,
        volume_step=spec.volume_step, volume_max=spec.volume_max, contract_size=spec.trade_contract_size,
        leverage=account.leverage,
    )
    if not sizing.accepted:
        return EntryRejected(SkipReason.SIZING_REJECTED, f"balance {balance!r}, fill {fill!r}, SL {sl!r}",
                             f"{SKIP_REASON_FA[SkipReason.SIZING_REJECTED]} {sizing.reason_fa}", take_profit=tp,
                             sizing=sizing, **known)
    return OpenPosition(
        cand=cand, t=t, entry_index=i, entry=fill, entry_bid_open=op, sl=sl, tp=tp, volume=sizing.volume,
        risk_amount=sizing.actual_risk, balance_before=balance, sign=1.0 if buy else -1.0,
        commission=commission(sizing.volume, commission_per_lot_per_side), spread_entry_pts=pts,
        flags=[] if src == "historical" else [f"entry_spread_{src}"], warnings=list(sizing.warnings), sizing=sizing,
        spread_source=src,
    )


def exit_hit(sign: float, sl: float, tp: float, op: float, hi: float, lo: float,
             spr: float) -> tuple[ExitReason, float] | None:
    """Exit of an open trade inside one bar (module docstring, rule 2): SL before TP, gap variants at the
    open; longs on the bid, shorts on the ask (bar + ``spr``). ``None`` = still open after this bar."""
    if sign > 0:
        if op <= sl:
            return ExitReason.SL_GAP, op
        if op >= tp:
            return ExitReason.TP_GAP, op
        if lo <= sl:
            return ExitReason.SL, sl
        if hi >= tp:
            return ExitReason.TP, tp
        return None
    a = op + spr
    if a >= sl:
        return ExitReason.SL_GAP, a
    if a <= tp:
        return ExitReason.TP_GAP, a
    if hi + spr >= sl:
        return ExitReason.SL, sl
    if lo + spr <= tp:
        return ExitReason.TP, tp
    return None


def check_exit(tr: OpenPosition, i: int, L: BarLists) -> tuple[ExitReason, float] | None:
    """:func:`exit_hit` for bar ``i`` of an open trade (from its entry bar on); flags a ``missing`` gap
    crossed while the trade is open."""
    if i > tr.entry_index and L["missing"][i - 1] and "missing_gap_during_trade" not in tr.flags:
        tr.flags.append("missing_gap_during_trade")
    return exit_hit(tr.sign, tr.sl, tr.tp, L["open"][i], L["high"][i], L["low"][i], L["spr"][i])


def exit_time_ns(reason: ExitReason, bar_open_ns: int) -> int:
    """Gap exits are known at the bar OPEN, intrabar hits and end-of-period closes at the bar CLOSE."""
    return bar_open_ns if reason in (ExitReason.SL_GAP, ExitReason.TP_GAP) else bar_open_ns + H1_NS


def liquidation_price(sign: float, close: float, spr: float) -> float:
    """Closing price at a bar close: longs at the bid close, shorts at the ask close (``close + spr``)."""
    return close if sign > 0 else close + spr


def settle(
    tr: OpenPosition,
    i: int,
    price: float,
    reason: ExitReason,
    exit_ns: int,
    L: BarLists,
    *,
    vpu: float,
    balance: float,
    window_index: int,
    trade_index: int,
) -> Trade:
    """The closed trade: ``gross = (exit - entry) * dir * volume * vpu``, ``net = gross - commission``,
    ``R = net / risk_amount``; ``balance_after = balance + net``; weekend and exit-spread flags."""
    gross = (price - tr.entry) * tr.sign * tr.volume * vpu
    net = gross - tr.commission
    balance_after = balance + net
    entry_ns = L["times"][tr.entry_index]
    weekend = held_over_weekend(entry_ns, exit_ns)
    flags = list(tr.flags)
    if weekend:
        flags.append("held_over_weekend")
    spread_exit: int | None = None
    if tr.sign < 0:  # sells exit on the ask side: the exit bar's spread was used
        spread_exit = L["spr_pts"][i]
        src = spread_source(L, i)
        if src != "historical":
            flags.append(f"exit_spread_{src}")
    cand = tr.cand
    return Trade(
        window_index=window_index, trade_index=trade_index, direction=cand.direction, setup_type=cand.setup_slug,
        line=cand.line, pattern=cand.pattern, confirmation_bar_time=cand.confirmation_bar_open_utc,
        decision_time=cand.decision_time_utc, entry_time=ns_to_dt(entry_ns), entry=tr.entry,
        entry_bid_open=tr.entry_bid_open, stop_loss=tr.sl, take_profit=tr.tp, rr=cand.rr, volume=tr.volume,
        risk_amount=tr.risk_amount, balance_before=tr.balance_before, exit_bar_time=ns_to_dt(L["times"][i]),
        exit_time=ns_to_dt(exit_ns), exit_price=price, exit_reason=reason, exit_reason_fa=EXIT_REASON_FA[reason],
        gross_pnl=gross, commission=tr.commission, net_pnl=net,
        r_multiple=net / tr.risk_amount if tr.risk_amount > 0 else None, balance_after=balance_after,
        bars_held=i - tr.entry_index + 1, spread_at_entry_points=tr.spread_entry_pts,
        spread_at_exit_points=spread_exit, held_over_weekend=weekend, flags=flags,
        sizing_warnings_fa=tr.warnings, reason_fa=cand.reason_fa, indicators=dict(cand.extra),
    )


def simulate_window(
    bars: BarArrays,
    window: Window,
    candidate_at: CandidateProvider,
    *,
    spec: SymbolSpec,
    account: AccountSettings,
    cost_model: CostModel,
    progress: Callable[[int], None] | None = None,
    cancel_event: threading.Event | None = None,
    progress_every: int = PROGRESS_EVERY,
) -> WindowResult:
    """Simulate one window starting flat with ``account.balance``. See the module docstring for the rules."""
    dev = is_dev_mode()
    wi = window.index
    i0, i1 = window_bar_range(bars.times_ns, window)
    L = bars.lists()
    C, SPR, SPTS, FILLED, UNFILLED, TIMES = L["close"], L["spr"], L["spr_pts"], L["filled"], L["unfilled"], L["times"]
    FALLBACK = L["fallback"]
    vpu = value_per_price_unit(spec)
    per_side = cost_model.commission_per_lot_per_side
    initial = float(account.balance)
    balance = initial
    close_dts = pd.to_datetime(bars.times_ns[i0:i1] + H1_NS, utc=True).to_pydatetime().tolist() if i1 > i0 else []

    trades: list[Trade] = []
    skipped: list[SkippedCandidate] = []
    equity: list[EquityPoint] = []
    construct = EquityPoint.model_construct
    if i1 > i0:
        equity.append(construct(time=ns_to_dt(TIMES[i0]), balance=balance, equity=balance))
    trade: OpenPosition | None = None
    pending: tuple[SignalCandidate, int] | None = None
    n_candidates = 0
    stopped: str | None = None
    weekend_holds = 0

    if dev:
        logger.debug("bt window %d %s..%s: bars %d..%d (%d) balance=%.2f risk=%.2f%% lev=%d rr=%g "
                     "commission=%g/lot/side vpu=%g point=%g", wi, window.start.isoformat(), window.end.isoformat(),
                     i0, i1, i1 - i0, balance, account.risk_pct, account.leverage, account.rr, per_side, vpu,
                     spec.point)

    def skip(cand: SignalCandidate, reason: SkipReason, detail: str | None, reason_fa: str | None = None) -> None:
        skipped.append(SkippedCandidate(
            window_index=wi, time=cand.decision_time_utc, confirmation_bar_time=cand.confirmation_bar_open_utc,
            direction=cand.direction, setup_type=cand.setup_slug, reason=reason,
            reason_fa=reason_fa or SKIP_REASON_FA[reason], detail=detail,
        ))
        if dev:
            logger.debug("bt w%d %s: %s %s skipped: %s (%s)", wi, cand.decision_time_utc.isoformat(), cand.direction,
                         cand.setup_slug, reason.value, detail)

    def close_trade(tr: OpenPosition, i: int, price: float, reason: ExitReason, exit_ns: int) -> None:
        nonlocal balance, weekend_holds
        balance_before_exit = balance
        closed = settle(tr, i, price, reason, exit_ns, L, vpu=vpu, balance=balance, window_index=wi,
                        trade_index=len(trades))
        balance = closed.balance_after
        weekend_holds += closed.held_over_weekend
        trades.append(closed)
        if dev:
            logger.debug("bt w%d exit %s %s @ bar %s: %s price=%.5f entry=%.5f vol=%g gross=%.4f commission=%.4f "
                         "net=%.4f R=%.4f balance %.2f -> %.2f weekend=%s", wi, closed.direction, closed.setup_type,
                         ns_to_dt(TIMES[i]).isoformat(), reason.value, price, tr.entry, tr.volume, closed.gross_pnl,
                         tr.commission, closed.net_pnl, closed.r_multiple or 0.0, balance_before_exit, balance,
                         closed.held_over_weekend)

    def enter(cand: SignalCandidate, t: int, i: int) -> OpenPosition | None:
        opened = open_position(cand, t, i, L, balance=balance, spec=spec, account=account,
                               commission_per_lot_per_side=per_side)
        if isinstance(opened, EntryRejected):
            skip(cand, opened.reason, opened.detail, opened.reason_fa)
            return None
        if dev:
            sizing = opened.sizing
            assert sizing is not None
            logger.debug("bt w%d fill %s %s @ %s: bid open=%.5f spread=%d pts (%.5f)%s fill=%.5f SL=%.5f TP=%.5f "
                         "balance=%.2f risk=%.2f vol=%g (raw %.6f) actual_risk=%.4f margin=%.2f commission=%.4f", wi,
                         cand.direction, cand.setup_slug, ns_to_dt(TIMES[i]).isoformat(), opened.entry_bid_open,
                         SPTS[i], SPR[i], "" if opened.spread_source == "historical" else f" [{opened.spread_source}]",
                         opened.entry, opened.sl, opened.tp, balance, sizing.risk_amount, sizing.volume,
                         sizing.raw_volume, sizing.actual_risk, sizing.margin, opened.commission)
        return opened

    for i in range(i0, i1):
        k = i - i0
        if k % progress_every == 0:
            if cancel_event is not None and cancel_event.is_set():
                raise BacktestCancelled(f"window {wi} cancelled at bar {k}")
            if progress is not None and k:
                progress(k)
        # 1. entry of the candidate accepted at the close of bar i-1
        if pending is not None:
            cand, t = pending
            pending = None
            trade = enter(cand, t, i)
        # 2. exits (from the entry bar itself on)
        if trade is not None:
            hit = check_exit(trade, i, L)
            if hit is not None:
                reason, price = hit
                close_trade(trade, i, price, reason, exit_time_ns(reason, TIMES[i]))
                trade = None
        # 3. mark to market at the close
        if trade is not None:
            mark = liquidation_price(trade.sign, C[i], SPR[i])
            eq = balance + (mark - trade.entry) * trade.sign * trade.volume * vpu - trade.commission
        else:
            eq = balance
        equity.append(construct(time=close_dts[k], balance=balance, equity=eq))
        if balance <= 0:
            stopped = "balance_depleted"
            if dev:
                logger.debug("bt w%d: balance %.2f <= 0 after bar %s -> window stopped", wi, balance,
                             ns_to_dt(TIMES[i]).isoformat())
            i1_eff = i + 1
            break
        # 4. decision at the close of bar i
        cand = candidate_at(i, trade is not None)
        if cand is not None:
            n_candidates += 1
            if dev:
                logger.debug("bt w%d decision @ %s: candidate %s %s line=%s ref=%.5f SL=%.5f open_trade=%s", wi,
                             cand.decision_time_utc.isoformat(), cand.direction, cand.setup_slug, cand.line,
                             cand.reference_price, cand.stop_loss, trade is not None)
            if trade is not None:
                skip(cand, SkipReason.POSITION_OPEN,
                     f"open {trade.cand.direction} since {ns_to_dt(TIMES[trade.entry_index]).isoformat()}")
            else:
                blocked = entry_block(L, i, stop=i1, window_end=window.end)
                if blocked is not None:
                    skip(cand, blocked.reason, blocked.detail, blocked.reason_fa)
                else:
                    pending = (cand, i)
    else:
        i1_eff = i1

    if trade is not None and stopped is None:
        last = i1_eff - 1
        close_trade(trade, last, liquidation_price(trade.sign, C[last], SPR[last]), ExitReason.END_OF_PERIOD,
                    exit_time_ns(ExitReason.END_OF_PERIOD, TIMES[last]))
        trade = None
        # the last equity point already holds this liquidation value; keep balance/equity consistent
        equity[-1] = construct(time=equity[-1].time, balance=balance, equity=balance)

    bars_done = i1_eff - i0
    filled = sum(FILLED[i0:i1_eff])
    unfilled = sum(UNFILLED[i0:i1_eff])
    fallback_bars = sum(FALLBACK[i0:i1_eff])
    if progress is not None and bars_done:
        progress(bars_done)
    result = WindowResult(
        window=window, initial_balance=initial, final_balance=balance, bars=bars_done,
        first_bar_time=ns_to_dt(TIMES[i0]) if bars_done else None,
        last_bar_time=ns_to_dt(TIMES[i1_eff - 1]) if bars_done else None,
        trades=trades, skipped=skipped, equity=equity, candidates=n_candidates, zero_spread_bars_filled=int(filled),
        zero_spread_bars_unfilled=int(unfilled), weekend_holds=int(weekend_holds), stopped_reason=stopped,
        spread_fallback_bars=int(fallback_bars),
    )
    if dev:
        logger.debug("bt window %d done: bars=%d candidates=%d trades=%d skipped=%d balance %.2f -> %.2f "
                     "zero_spread filled=%d fallback(%d pts)=%d unfilled=%d weekend_holds=%d stopped=%s", wi,
                     bars_done, n_candidates, len(trades), len(skipped), initial, balance, filled,
                     bars.spread.fallback_points, fallback_bars, unfilled, weekend_holds, stopped)
    return result


__all__ = [
    "BacktestCancelled",
    "BarArrays",
    "BarLists",
    "CandidateProvider",
    "EntryRejected",
    "OpenPosition",
    "SpreadSource",
    "check_exit",
    "entry_block",
    "exit_hit",
    "exit_time_ns",
    "held_over_weekend",
    "liquidation_price",
    "open_position",
    "scan_provider",
    "settle",
    "simulate_window",
    "spread_source",
    "value_per_price_unit",
    "window_bar_range",
]
