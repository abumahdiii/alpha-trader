"""Independent outcome of every scanned setup + "was it traded in the backtest?" (phase 5, HIGH-RISK).

Used by ``GET /chart/setups`` for the full setups table. Everything is computed with the SAME trade-rule
helpers as the backtest simulator (``simulator.entry_block`` / ``open_position`` / ``check_exit`` /
``settle`` / ``liquidation_price``), so an outcome can never follow different rules than a backtest trade.

Per-setup outcome (:func:`simulate_setup`)
------------------------------------------
Each setup is evaluated ON ITS OWN -- no "one open trade at a time" constraint, sized with the settings
balance (``account.balance``), never with a balance changed by other setups:

1. decision at the close of the confirmation bar ``t``: bar ``t+1`` not cached yet -> ``pending_entry``;
   a ``missing`` gap between ``t`` and ``t+1`` -> ``rejected`` (``missing_gap``);
2. entry at the open of ``t+1`` (the next cached bar, e.g. Sunday's open after a weekend): buy at the ask
   open (bid open + spread * point, zero spread causally filled, fallback spread before the first broker
   spread), sell at the bid open; gap through the stop, invalid levels, volume below the minimum or margin
   above the balance -> ``rejected`` with the simulator's Persian reason;
3. the trade is walked bar by bar from the entry bar itself: SL before TP, gap exits at the open, longs exit
   on the bid, shorts on the ask;
4. still open on the LAST cached bar -> closed at its close (long: bid close, short: ask close = close +
   spread * point), ``exit_reason = end_of_period`` and ``result = "end_of_data"`` («پایان داده»). This
   result is provisional: it changes when bars are appended, and it is counted separately in the summary.

Only data up to the exit bar is read (tests/test_setup_outcomes.py: truncation invariance). Outcomes are
fresh objects per call; candidates are never modified (``indicators`` is a copy of ``extra``).

Backtest flag (:func:`backtest_window_for_range`, :func:`run_range_backtest`, :func:`backtest_flags`)
---------------------------------------------------------------------------------------------------
The chart range ``[from, to]`` filters setups by decision time. The button «بک‌تست همین بازه» submits the
manual window of the bars whose decisions fall in that range::

    start = ceil_to_hour(from) - 1h        (confirmation bar of the first decision >= from)
    end   = floor_to_hour(to)              (exclusive; the last decision <= to has its bar opening at to - 1h)

clipped to ``[periods.earliest_start, periods.data_end]`` and validated with ``periods.manual_window`` --
exactly what ``POST /backtests`` does. That window is run with ``runner.run_backtest`` (default
``CostModel()``, the same account settings, the same full-history scan), and every setup is matched by its
confirmation bar: ``traded`` with the backtest trade's index and net P&L, or not traded with the reason of
the backtest's ``SkippedCandidate`` (``position_open`` -> «در بک‌تست پوزیشن دیگری باز بود ...»), or
«خارج از بازه بک‌تست» when the setup lies outside the (clipped) window. A range entirely before the
earliest start -> no window (``available = false``) with the Persian ``PeriodError`` message.

Worked example (buy TP, gold spec point 0.01, tick 0.01 / 1.00 -> 100 USD per 1.00 per lot, step 0.01;
balance 2500, risk 1 % = 25, rr 2): SL 1995.00, entry bar bid open 2000.00, spread 25 points -> entry =
ask = 2000.25; distance 5.25; TP = 2000.25 + 2 * 5.25 = 2010.75; volume = floor(25 / (5.25 * 100), 0.01)
= floor(0.047619) = 0.04; risk = 0.04 * 525 = 21.00; a bar with bid high 2011.00 -> exit 2010.75;
pnl_price = 10.50; net = 10.50 * 0.04 * 100 = 42.00; R = 42 / 21 = +2.0 (tests/test_setup_outcomes.py).

Summary (:func:`summarize_setups`)
----------------------------------
Counts ``total / accepted / rejected / pending_entry``; among the accepted: ``closed`` (TP/SL, incl. gaps),
``open_end_of_data``. Wins / losses / breakeven, ``win_rate`` (over closed, fraction 0..1), ``net_pnl``,
``gross_profit``, ``gross_loss``, ``profit_factor`` (+ ``profit_factor_infinite``) and ``avg_r`` come from
``metrics.compute_metrics`` over the CLOSED outcomes only; ``total_r = fsum(R)`` over them;
``open_net_pnl`` = the sum of the end-of-data P&L (not in wins/losses/PF). No drawdown / Sharpe / balance:
the setups overlap, so there is no single account curve.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from ..data.symbols import SymbolSpec
from ..logging_setup import get_logger, is_dev_mode
from ..storage.account_settings import AccountSettings
from ..strategy.base import WarmupTextsFa
from ..strategy.signal import SignalCandidate
from .history import HistoryData, ScanResult
from .metrics import compute_metrics
from .models import (
    CostModel,
    ExitReason,
    RunConfig,
    SkipReason,
    Trade,
    WindowResult,
)
from .periods import DEFAULT_WARMUP_TEXTS_FA, PeriodError, data_end, earliest_start, manual_window
from .runner import run_backtest
from .simulator import (
    BarArrays,
    EntryRejected,
    OpenPosition,
    check_exit,
    entry_block,
    exit_time_ns,
    liquidation_price,
    open_position,
    settle,
    value_per_price_unit,
)

logger = get_logger(__name__)

SetupStatus = Literal["accepted", "rejected", "pending_entry"]
SetupResult = Literal["tp", "sl", "end_of_data"]

END_OF_DATA_FA = "پایان داده؛ ستاپ تا آخرین کندل کش باز ماند و با قیمت بسته شدن آن کندل بسته شد (نتیجه موقت)"
POSITION_OPEN_IN_BACKTEST_FA = "در بک‌تست پوزیشن دیگری باز بود (حداکثر یک معامله باز)؛ این ستاپ معامله نشد."
OUTSIDE_BACKTEST_WINDOW_FA = "خارج از بازه بک‌تست"
NOT_REACHED_FA = "بک‌تست پیش از این ستاپ متوقف شد (موجودی تمام شد)."
H1 = pd.Timedelta(hours=1)

_RESULT: dict[ExitReason, SetupResult] = {
    ExitReason.TP: "tp", ExitReason.TP_GAP: "tp", ExitReason.SL: "sl", ExitReason.SL_GAP: "sl",
    ExitReason.END_OF_PERIOD: "end_of_data",
}


# ---------------------------------------------------------------------------------------------- per setup
@dataclass(frozen=True, eq=False)
class SetupOutcome:
    """Independent evaluation of one candidate. ``position``/``trade`` only when accepted."""

    candidate: SignalCandidate
    t: int  # confirmation bar index
    status: SetupStatus
    rejection: EntryRejected | None  # rejected / pending_entry
    position: OpenPosition | None
    trade: Trade | None
    result: SetupResult | None
    exit_index: int | None

    @property
    def pnl_price(self) -> float | None:
        """Price move in the trade's favour (exit - entry for a buy, entry - exit for a sell)."""
        if self.trade is None or self.position is None:
            return None
        return (self.trade.exit_price - self.trade.entry) * self.position.sign


def simulate_setup(
    bars: BarArrays,
    cand: SignalCandidate,
    t: int,
    *,
    spec: SymbolSpec,
    account: AccountSettings,
    cost_model: CostModel,
) -> SetupOutcome:
    """Outcome of ``cand`` (confirmation bar ``t``) on its own, with the settings balance (module docstring)."""
    L = bars.lists()
    times = L["times"]
    n = len(times)
    conf_ns = int(pd.Timestamp(cand.confirmation_bar_open_utc).as_unit("ns").value)
    if not 0 <= t < n or times[t] != conf_ns:
        raise ValueError(f"bar {t} is not the confirmation bar {cand.confirmation_bar_open_utc.isoformat()}")
    blocked = entry_block(L, t, stop=n)
    if blocked is not None:
        status: SetupStatus = "pending_entry" if blocked.reason is SkipReason.ENTRY_OUTSIDE_WINDOW else "rejected"
        return _log(SetupOutcome(cand, t, status, blocked, None, None, None, None))
    opened = open_position(cand, t, t + 1, L, balance=float(account.balance), spec=spec, account=account,
                           commission_per_lot_per_side=cost_model.commission_per_lot_per_side)
    if isinstance(opened, EntryRejected):
        return _log(SetupOutcome(cand, t, "rejected", opened, None, None, None, None))
    vpu = value_per_price_unit(spec)
    exit_at: tuple[int, ExitReason, float] | None = None
    for i in range(opened.entry_index, n):
        hit = check_exit(opened, i, L)
        if hit is not None:
            exit_at = (i, hit[0], hit[1])
            break
    if exit_at is None:  # open on the last cached bar: closed at its close, labelled "end of data"
        last = n - 1
        exit_at = (last, ExitReason.END_OF_PERIOD, liquidation_price(opened.sign, L["close"][last], L["spr"][last]))
    i, reason, price = exit_at
    trade = settle(opened, i, price, reason, exit_time_ns(reason, times[i]), L, vpu=vpu,
                   balance=float(account.balance), window_index=0, trade_index=0)
    return _log(SetupOutcome(cand, t, "accepted", None, opened, trade, _RESULT[reason], i))


def _log(outcome: SetupOutcome) -> SetupOutcome:
    if is_dev_mode():
        c = outcome.candidate
        if outcome.trade is None:
            rej = outcome.rejection
            logger.debug("setup outcome %s %s %s decision %s: %s %s (%s)", c.symbol, c.direction, c.setup_slug,
                         c.decision_time_utc.isoformat(), outcome.status, rej.reason.value if rej else "-",
                         rej.detail if rej else "-")
        else:
            tr, pos = outcome.trade, outcome.position
            assert pos is not None
            logger.debug("setup outcome %s %s %s decision %s: entry=%.5f (bid open %.5f, spread %d pts %s) SL=%.5f "
                         "TP=%.5f vol=%g risk=%.4f -> %s/%s exit %s @ %.5f pnl_price=%.5f gross=%.4f commission=%.4f "
                         "net=%.4f R=%s bars=%d", c.symbol, c.direction, c.setup_slug, c.decision_time_utc.isoformat(),
                         tr.entry, tr.entry_bid_open, tr.spread_at_entry_points, pos.spread_source, tr.stop_loss,
                         tr.take_profit, tr.volume, tr.risk_amount, outcome.result, tr.exit_reason.value,
                         tr.exit_time.isoformat(), tr.exit_price, outcome.pnl_price or 0.0, tr.gross_pnl,
                         tr.commission, tr.net_pnl, tr.r_multiple, tr.bars_held)
    return outcome


def confirmation_indices(bars: BarArrays, candidates: Sequence[SignalCandidate]) -> list[int]:
    """Bar index ``t`` of each candidate's confirmation bar (exact match required)."""
    conf = np.array([pd.Timestamp(c.confirmation_bar_open_utc).as_unit("ns").value for c in candidates],
                    dtype=np.int64)
    idx = np.searchsorted(bars.times_ns, conf)
    if len(conf) and (np.any(idx >= len(bars.times_ns)) or np.any(bars.times_ns[np.minimum(idx, len(bars) - 1)] != conf)):
        raise ValueError("a candidate's confirmation bar is not in the bar arrays")
    return idx.tolist()


def simulate_setups(bars: BarArrays, candidates: Sequence[SignalCandidate], *, spec: SymbolSpec,
                    account: AccountSettings, cost_model: CostModel) -> list[SetupOutcome]:
    return [simulate_setup(bars, c, t, spec=spec, account=account, cost_model=cost_model)
            for c, t in zip(candidates, confirmation_indices(bars, candidates), strict=True)]


# ---------------------------------------------------------------------------------------------- summary
def summarize_setups(outcomes: Sequence[SetupOutcome], balance: float) -> dict[str, Any]:
    """Counts + results of the independent outcomes (module docstring). ``balance`` only feeds
    ``compute_metrics``' validation (> 0); no balance-based value is returned."""
    accepted = [o for o in outcomes if o.status == "accepted"]
    closed = [o.trade for o in accepted if o.result in ("tp", "sl") and o.trade is not None]
    open_end = [o.trade for o in accepted if o.result == "end_of_data" and o.trade is not None]
    m = compute_metrics(closed, [], balance)
    r_values = [float(tr.r_multiple) for tr in closed if tr.r_multiple is not None and math.isfinite(tr.r_multiple)]
    summary = {
        "total": len(outcomes),
        "accepted": len(accepted),
        "rejected": sum(o.status == "rejected" for o in outcomes),
        "pending_entry": sum(o.status == "pending_entry" for o in outcomes),
        "closed": len(closed),
        "wins": m.win_count,
        "losses": m.loss_count,
        "breakeven": m.breakeven_count,
        "open_end_of_data": len(open_end),
        "win_rate": m.win_rate,
        "net_pnl": m.net_profit,
        "gross_profit": m.gross_profit,
        "gross_loss": m.gross_loss,
        "profit_factor": m.profit_factor,
        "profit_factor_infinite": m.profit_factor_infinite,
        "total_r": math.fsum(r_values),
        "avg_r": m.avg_r,
        "r_count": m.r_count,
        "open_net_pnl": math.fsum(tr.net_pnl for tr in open_end),
    }
    if is_dev_mode():
        logger.debug("setups summary: %s", summary)
    return summary


def counts_only_summary(statuses: Sequence[str]) -> dict[str, Any]:
    """Summary when no outcome could be computed (e.g. no symbol spec): counts, every result null / 0."""
    return {
        "total": len(statuses), "accepted": sum(s == "accepted" for s in statuses),
        "rejected": sum(s == "rejected" for s in statuses), "pending_entry": sum(s == "pending_entry" for s in statuses),
        "closed": 0, "wins": 0, "losses": 0, "breakeven": 0, "open_end_of_data": 0, "win_rate": None,
        "net_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": None,
        "profit_factor_infinite": False, "total_r": 0.0, "avg_r": None, "r_count": 0, "open_net_pnl": 0.0,
    }


# ---------------------------------------------------------------------------------------------- backtest flag
@dataclass(frozen=True)
class RangeWindow:
    """The manual backtest window of a chart range (what «بک‌تست همین بازه» submits)."""

    available: bool
    start: datetime | None
    end: datetime | None
    requested_start: datetime | None
    requested_end: datetime | None
    clipped: bool = False
    note_fa: str | None = None
    code: str | None = None
    message_fa: str | None = None


def _fmt(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%d %H:%M UTC")


def backtest_window_for_range(
    range_from: pd.Timestamp | None,
    range_to: pd.Timestamp | None,
    h1_times_ns: np.ndarray,
    h4_times_ns: np.ndarray,
    first_valid: int | None,
    margin_h4_bars: int,
    *,
    texts: WarmupTextsFa | None = None,
) -> RangeWindow:
    """Window for the decision range ``[range_from, range_to]`` (module docstring), clipped and validated
    exactly like ``POST /backtests`` (``periods.earliest_start`` + ``periods.manual_window``) with the strategy's
    warm-up margin (``Strategy.warmup_margin_h4_bars``) and wording (``Strategy.warmup_texts_fa``)."""
    if range_from is None or range_to is None or not len(h1_times_ns):
        return RangeWindow(False, None, None, None, None, code="no_data", message_fa="داده‌ای برای بک‌تست در کش نیست.")
    start = range_from.ceil("h") - H1
    end = range_to.floor("h")
    req = (start.to_pydatetime(), end.to_pydatetime())
    warm = int(margin_h4_bars)
    tx = texts if texts is not None else DEFAULT_WARMUP_TEXTS_FA
    try:
        earliest = earliest_start(h1_times_ns, h4_times_ns, first_valid, warm, texts=tx)
        last = data_end(h1_times_ns)
        if end <= earliest:  # the whole range is before the earliest start: the POST error itself
            manual_window(start, end, h1_times_ns, earliest, warmup_bars=warm, texts=tx)
            raise PeriodError("window_too_early", "بازه قبل از اولین زمان مجاز بک‌تست است.")  # pragma: no cover
        if start >= last:
            raise PeriodError("window_beyond_data", f"بازه بعد از آخرین داده کش ({_fmt(last)}) است.")
        notes: list[str] = []
        if start < earliest:
            start = earliest
            notes.append(f"ابتدای بازه بک‌تست به اولین زمان مجاز ({_fmt(earliest)}) منتقل شد ({tx.clip_reason}).")
        if end > last:
            end = last
            notes.append(f"انتهای بازه بک‌تست به آخرین داده کش ({_fmt(last)}) محدود شد.")
        manual_window(start, end, h1_times_ns, earliest, warmup_bars=warm, texts=tx)
    except PeriodError as exc:
        if is_dev_mode():
            logger.debug("setups backtest window %s..%s unavailable: %s", req[0].isoformat(), req[1].isoformat(),
                         exc.code)
        return RangeWindow(False, None, None, *req, code=exc.code, message_fa=exc.message_fa)
    window = RangeWindow(True, start.to_pydatetime(), end.to_pydatetime(), *req, clipped=bool(notes),
                         note_fa=" ".join(notes) or None)
    if is_dev_mode():
        logger.debug("setups backtest window for range %s..%s: %s..%s clipped=%s", range_from.isoformat(),
                     range_to.isoformat(), window.start.isoformat(), window.end.isoformat(), window.clipped)
    return window


def run_range_backtest(
    window: RangeWindow,
    history: HistoryData,
    scan: ScanResult,
    bars: BarArrays,
    *,
    account: AccountSettings,
    params: dict[str, Any],
    params_hash: str,
    params_version: int | None,
    strategy_name: str,
    strategy_version: int,
    cost_model: CostModel,
    provisional: bool = True,
) -> WindowResult:
    """The manual backtest of ``window`` exactly as the job worker runs it (``runner.run_backtest``; the strategy
    is the one that made ``scan``, whose identity goes into the config)."""
    assert window.available and window.start is not None and window.end is not None
    ident = scan.identity
    config = RunConfig(symbol=history.symbol, mode="manual", start=window.start, end=window.end, cost_model=cost_model,
                       account=account, strategy_name=strategy_name, strategy_version=strategy_version,
                       strategy_sha256=None if ident is None else ident.sha256,
                       strategy_source=None if ident is None else ident.source, params=params,
                       params_version=params_version, params_hash=params_hash, provisional=provisional)
    result = run_backtest(config, history, scan, bars=bars)
    return result.windows[0]


@dataclass(frozen=True)
class BacktestFlag:
    traded: bool
    reason: str | None
    reason_fa: str | None
    trade_index: int | None
    net_pnl: float | None


def backtest_flags(result: WindowResult, candidates: Sequence[SignalCandidate]) -> list[BacktestFlag]:
    """Match every candidate with the backtest window by its confirmation bar (unique per window)."""
    trades = {t.confirmation_bar_time: t for t in result.trades}
    skipped = {s.confirmation_bar_time: s for s in result.skipped}
    start, end = result.window.start, result.window.end
    flags: list[BacktestFlag] = []
    for cand in candidates:
        key = cand.confirmation_bar_open_utc
        if key in trades:
            tr = trades[key]
            flags.append(BacktestFlag(True, None, None, tr.trade_index, tr.net_pnl))
        elif key in skipped:
            sk = skipped[key]
            reason_fa = POSITION_OPEN_IN_BACKTEST_FA if sk.reason is SkipReason.POSITION_OPEN else sk.reason_fa
            flags.append(BacktestFlag(False, sk.reason.value, reason_fa, None, None))
        elif not start <= key < end:
            flags.append(BacktestFlag(False, "outside_window", OUTSIDE_BACKTEST_WINDOW_FA, None, None))
        else:  # the window stopped early (balance depleted) before this decision
            flags.append(BacktestFlag(False, "not_reached", NOT_REACHED_FA, None, None))
    if is_dev_mode():
        traded = sum(f.traded for f in flags)
        reasons: dict[str, int] = {}
        for f in flags:
            if f.reason:
                reasons[f.reason] = reasons.get(f.reason, 0) + 1
        logger.debug("setups backtest flags %s..%s: %d setups, %d traded (backtest trades %d), not traded %s",
                     start.isoformat(), end.isoformat(), len(flags), traded, len(result.trades), reasons)
    return flags


def span_indices(outcomes: Sequence[SetupOutcome], bars: BarArrays) -> tuple[int, int] | None:
    """``[first confirmation bar, last bar used]`` of the outcomes (for the spread label), or ``None``."""
    if not outcomes:
        return None
    lo = min(o.t for o in outcomes)
    hi = max(o.exit_index if o.exit_index is not None else min(o.t + 1, len(bars) - 1) for o in outcomes)
    return lo, hi


__all__ = [
    "END_OF_DATA_FA",
    "OUTSIDE_BACKTEST_WINDOW_FA",
    "POSITION_OPEN_IN_BACKTEST_FA",
    "BacktestFlag",
    "RangeWindow",
    "SetupOutcome",
    "backtest_flags",
    "backtest_window_for_range",
    "confirmation_indices",
    "counts_only_summary",
    "run_range_backtest",
    "simulate_setup",
    "simulate_setups",
    "span_indices",
    "summarize_setups",
]
