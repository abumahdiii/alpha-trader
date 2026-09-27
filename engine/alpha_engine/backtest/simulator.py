"""Bar-by-bar trade simulation over the H1 bars of ONE window (phase-4 decisions 1-5).

Decisions come from a *candidate provider* ``candidate_at(i, has_open_trade)``: in production the
precomputed full-history ``StdDevChannelStrategy.scan`` (``history.scan_full_history``) indexed by the
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
   * levels ``resolve_trade_levels(candidate, fill)`` (TP = fill +/- rr * |fill - SL|); a ValueError
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
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..risk.sizing import size_position
from ..storage.account_settings import AccountSettings
from ..strategies.stddev_channel.levels import resolve_trade_levels
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


@dataclass
class _Open:
    cand: SignalCandidate
    t: int
    entry_index: int
    entry: float
    entry_bid_open: float
    sl: float
    tp: float
    volume: float
    risk_amount: float
    balance_before: float
    sign: float
    commission: float
    spread_entry_pts: int
    flags: list[str]
    warnings: list[str]


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
    n_total = len(bars)
    L = bars.lists()
    O, H, LO, C = L["open"], L["high"], L["low"], L["close"]
    SPR, SPTS, FILLED, UNFILLED, MISSING, TIMES = (L["spr"], L["spr_pts"], L["filled"], L["unfilled"], L["missing"],
                                                  L["times"])
    FALLBACK = L["fallback"]
    vpu =spec.trade_tick_value / spec.trade_tick_size if spec.trade_tick_size else 0.0
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
    trade: _Open | None = None
    pending: tuple[SignalCandidate, int] | None = None
    n_candidates = 0
    stopped: str | None = None
    weekend_holds = 0

    if dev:
        logger.debug("bt window %d %s..%s: bars %d..%d (%d) balance=%.2f risk=%.2f%% lev=%d rr=%g "
                     "commission=%g/lot/side vpu=%g point=%g", wi, window.start.isoformat(), window.end.isoformat(), i0, i1, i1 - i0, balance,
                     account.risk_pct, account.leverage, account.rr, per_side, vpu, spec.point)

    def skip(cand: SignalCandidate, reason: SkipReason, detail: str | None, reason_fa: str | None = None) -> None:
        skipped.append(SkippedCandidate(
            window_index=wi, time=cand.decision_time_utc, confirmation_bar_time=cand.confirmation_bar_open_utc,
            direction=cand.direction, setup_type=cand.setup.value, reason=reason,
            reason_fa=reason_fa or SKIP_REASON_FA[reason], detail=detail,
        ))
        if dev:
            logger.debug("bt w%d %s: %s %s skipped: %s (%s)", wi, cand.decision_time_utc.isoformat(), cand.direction,
                         cand.setup.value, reason.value, detail)

    def close_trade(tr: _Open, i: int, price: float, reason: ExitReason, exit_ns: int) -> None:
        nonlocal balance, weekend_holds
        gross = (price - tr.entry) * tr.sign * tr.volume * vpu
        net = gross - tr.commission
        balance_before_exit = balance
        balance = balance + net
        entry_ns = TIMES[tr.entry_index]
        weekend = held_over_weekend(entry_ns, exit_ns)
        weekend_holds += weekend
        flags = list(tr.flags)
        if weekend:
            flags.append("held_over_weekend")
        spread_exit: int | None = None
        if tr.sign < 0:  # sells exit on the ask side: the exit bar's spread was used
            spread_exit = SPTS[i]
            if FILLED[i]:
                flags.append("exit_spread_filled")
            elif FALLBACK[i]:
                flags.append("exit_spread_fallback")
            elif UNFILLED[i]:
                flags.append("exit_spread_zero")
        cand = tr.cand
        trades.append(Trade(
            window_index=wi, trade_index=len(trades), direction=cand.direction, setup_type=cand.setup.value,
            line=cand.line, pattern=cand.pattern, confirmation_bar_time=cand.confirmation_bar_open_utc,
            decision_time=cand.decision_time_utc, entry_time=ns_to_dt(entry_ns), entry=tr.entry,
            entry_bid_open=tr.entry_bid_open, stop_loss=tr.sl, take_profit=tr.tp, rr=cand.rr, volume=tr.volume,
            risk_amount=tr.risk_amount, balance_before=tr.balance_before, exit_bar_time=ns_to_dt(TIMES[i]),
            exit_time=ns_to_dt(exit_ns), exit_price=price, exit_reason=reason, exit_reason_fa=EXIT_REASON_FA[reason],
            gross_pnl=gross, commission=tr.commission, net_pnl=net,
            r_multiple=net / tr.risk_amount if tr.risk_amount > 0 else None, balance_after=balance,
            bars_held=i - tr.entry_index + 1, spread_at_entry_points=tr.spread_entry_pts,
            spread_at_exit_points=spread_exit, held_over_weekend=weekend, flags=flags,
            sizing_warnings_fa=tr.warnings, reason_fa=cand.reason_fa, indicators=dict(cand.extra),
        ))
        if dev:
            logger.debug("bt w%d exit %s %s @ bar %s: %s price=%.5f entry=%.5f vol=%g gross=%.4f commission=%.4f "
                         "net=%.4f R=%.4f balance %.2f -> %.2f weekend=%s", wi, cand.direction, cand.setup.value,
                         ns_to_dt(TIMES[i]).isoformat(), reason.value, price, tr.entry, tr.volume, gross, tr.commission,
                         net, trades[-1].r_multiple or 0.0, balance_before_exit, balance, weekend)

    def enter(cand: SignalCandidate, t: int, i: int) -> _Open | None:
        op = O[i]
        spr = SPR[i]
        ask_open = op + spr
        buy = cand.direction == "buy"
        sl = cand.stop_loss
        fill = ask_open if buy else op
        if (op <= sl) if buy else (ask_open >= sl):
            side = f"bid open {op!r} <= SL {sl!r}" if buy else f"ask open {ask_open!r} >= SL {sl!r}"
            skip(cand, SkipReason.GAP_THROUGH_STOP, f"{side} (spread {SPTS[i]} points)")
            return None
        try:
            levels = resolve_trade_levels(cand, fill)
        except ValueError as exc:
            skip(cand, SkipReason.INVALID_LEVELS, f"fill {fill!r}: {exc}")
            return None
        sizing = size_position(
            balance=balance, risk_pct=account.risk_pct, entry=fill, stop_loss=levels.stop_loss,
            tick_value=spec.trade_tick_value, tick_size=spec.trade_tick_size, volume_min=spec.volume_min,
            volume_step=spec.volume_step, volume_max=spec.volume_max, contract_size=spec.trade_contract_size,
            leverage=account.leverage,
        )
        if not sizing.accepted:
            skip(cand, SkipReason.SIZING_REJECTED, f"balance {balance!r}, fill {fill!r}, SL {levels.stop_loss!r}",
                 reason_fa=f"{SKIP_REASON_FA[SkipReason.SIZING_REJECTED]} {sizing.reason_fa}")
            return None
        flags: list[str] = []
        if FILLED[i]:
            flags.append("entry_spread_filled")
        elif FALLBACK[i]:
            flags.append("entry_spread_fallback")
        elif UNFILLED[i]:
            flags.append("entry_spread_zero")
        tr = _Open(
            cand=cand, t=t, entry_index=i, entry=fill, entry_bid_open=op, sl=levels.stop_loss,
            tp=levels.take_profit, volume=sizing.volume, risk_amount=sizing.actual_risk, balance_before=balance,
            sign=1.0 if buy else -1.0, commission=commission(sizing.volume, per_side), spread_entry_pts=SPTS[i],
            flags=flags, warnings=list(sizing.warnings),
        )
        if dev:
            logger.debug("bt w%d fill %s %s @ %s: bid open=%.5f spread=%d pts (%.5f)%s fill=%.5f SL=%.5f TP=%.5f "
                         "balance=%.2f risk=%.2f vol=%g (raw %.6f) actual_risk=%.4f margin=%.2f commission=%.4f", wi,
                         cand.direction, cand.setup.value, ns_to_dt(TIMES[i]).isoformat(), op, SPTS[i], spr,
                         " [filled]" if FILLED[i] else (" [fallback]" if FALLBACK[i] else ""), fill, tr.sl, tr.tp, balance, sizing.risk_amount,
                         sizing.volume, sizing.raw_volume, sizing.actual_risk, sizing.margin, tr.commission)
        return tr

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
            if i > trade.entry_index and MISSING[i - 1] and "missing_gap_during_trade" not in trade.flags:
                trade.flags.append("missing_gap_during_trade")
            op, hi, lo = O[i], H[i], LO[i]
            sl, tp = trade.sl, trade.tp
            reason: ExitReason | None = None
            if trade.sign > 0:
                if op <= sl:
                    reason, price = ExitReason.SL_GAP, op
                elif op >= tp:
                    reason, price = ExitReason.TP_GAP, op
                elif lo <= sl:
                    reason, price = ExitReason.SL, sl
                elif hi >= tp:
                    reason, price = ExitReason.TP, tp
            else:
                spr = SPR[i]
                a = op + spr
                if a >= sl:
                    reason, price = ExitReason.SL_GAP, a
                elif a <= tp:
                    reason, price = ExitReason.TP_GAP, a
                elif hi + spr >= sl:
                    reason, price = ExitReason.SL, sl
                elif lo + spr <= tp:
                    reason, price = ExitReason.TP, tp
            if reason is not None:
                gap = reason in (ExitReason.SL_GAP, ExitReason.TP_GAP)
                close_trade(trade, i, price, reason, TIMES[i] if gap else TIMES[i] + H1_NS)
                trade = None
        # 3. mark to market at the close
        if trade is not None:
            mark = C[i] if trade.sign > 0 else C[i] + SPR[i]
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
                             cand.decision_time_utc.isoformat(), cand.direction, cand.setup.value, cand.line,
                             cand.reference_price, cand.stop_loss, trade is not None)
            if trade is not None:
                skip(cand, SkipReason.POSITION_OPEN,
                     f"open {trade.cand.direction} since {ns_to_dt(TIMES[trade.entry_index]).isoformat()}")
            elif i + 1 >= i1 or i + 1 >= n_total:
                skip(cand, SkipReason.ENTRY_OUTSIDE_WINDOW,
                     "no bar after the confirmation bar in the data" if i + 1 >= n_total else
                     f"entry bar {ns_to_dt(TIMES[i + 1]).isoformat()} >= window end {window.end.isoformat()}")
            elif MISSING[i]:
                skip(cand, SkipReason.MISSING_GAP,
                     f"{ns_to_dt(TIMES[i]).isoformat()} -> {ns_to_dt(TIMES[i + 1]).isoformat()}")
            else:
                pending = (cand, i)
    else:
        i1_eff = i1

    if trade is not None and stopped is None:
        last = i1_eff - 1
        price = C[last] if trade.sign > 0 else C[last] + SPR[last]
        close_trade(trade, last, price, ExitReason.END_OF_PERIOD, TIMES[last] + H1_NS)
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
    "CandidateProvider",
    "held_over_weekend",
    "scan_provider",
    "simulate_window",
    "window_bar_range",
]
