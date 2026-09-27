"""Backtest performance metrics (phase 4, HIGH-RISK financial math). Pure functions, no I/O.

Inputs are duck-typed (:class:`TradeLike`, :class:`EquityPointLike`, :class:`WindowSummaryLike`), so this
module does not depend on the simulator's models. All results are deterministic: sums use
``math.fsum`` and nothing depends on hash order or randomness.

Conventions
-----------
* Money is in account currency. ``eps = BREAKEVEN_EPS = 1e-9``.
* Timestamps are UTC. A naive ``datetime`` is taken as UTC; an aware one is converted to UTC.
* Trades are processed in ``exit_time`` order (stable sort, so equal exit times keep input order).
* The equity series must be chronological (non-decreasing ``time``); otherwise ``ValueError``.
* ``initial_balance`` must be finite and > 0; every ``net_pnl`` and ``equity`` must be finite;
  otherwise ``ValueError`` (garbage in must not produce a plausible-looking report).
* Undefined values are ``None``, never NaN/inf; :meth:`BacktestMetrics.to_dict` is JSON-safe.

Trade metrics (N = trade_count)
-------------------------------
* win  : ``net_pnl >  eps``;  loss: ``net_pnl < -eps``;  breakeven: ``|net_pnl| <= eps``.
  ``win_count + loss_count + breakeven_count == trade_count``.
* ``win_rate          = win_count / N``                       (a fraction 0..1; None if N == 0)
* ``gross_profit      = sum(net_pnl of wins)``                (>= 0)
* ``gross_loss        = |sum(net_pnl of losses)|``            (>= 0, a positive magnitude)
* ``net_profit        = sum(net_pnl of all trades)``          (0.0 if N == 0; breakeven trades within
  eps are included here, so ``net_profit`` may differ from ``gross_profit - gross_loss`` by < N * eps)
* ``net_profit_pct    = net_profit / initial_balance * 100``
* ``final_balance     = initial_balance + net_profit``
* ``profit_factor     = gross_profit / gross_loss``; None if ``gross_loss == 0``.
  ``profit_factor_infinite = True`` exactly when ``gross_profit > 0`` and there are no losses.
* ``expectancy        = net_profit / N``                      (currency per trade; None if N == 0)
* ``avg_win           = gross_profit / win_count``            (positive; None if no wins)
* ``avg_loss          = sum(net_pnl of losses) / loss_count`` (a NEGATIVE number; None if no losses)
* ``largest_win       = max(net_pnl of wins)``                (positive; None if no wins)
* ``largest_loss      = min(net_pnl of losses)``              (the most NEGATIVE pnl; None if no losses)
* ``max_consecutive_wins / _losses``: longest run in exit-time order. A breakeven trade ends BOTH runs.
* ``avg_r             = mean(r_multiple)`` over trades whose ``r_multiple`` is not None and finite
  (a NaN/inf R is treated as missing); ``r_count`` is how many were used. None if ``r_count == 0``.

Drawdown (mark-to-market equity series)
---------------------------------------
Running peak ``P`` starts at ``initial_balance`` (at no particular time), then for each point in order
``P(t) = max(P(t-1), equity(t))``::

    dd_abs(t) = P(t) - equity(t)
    max_drawdown_abs = max_t dd_abs(t)                       (0.0 for an empty series)
    max_drawdown_pct = max_t dd_abs(t) / P(t) * 100          (P(t) >= initial_balance > 0)

The two maxima are taken independently, so ``max_drawdown_pct`` can come from a DIFFERENT point than
``max_drawdown_abs`` (e.g. a 100 drop from a 1000 peak = 10 % beats a 200 drop from a 3000 peak =
6.67 %). ``max_drawdown_peak_time`` / ``max_drawdown_trough_time`` belong to the max ABSOLUTE
drawdown: trough = first point reaching the maximum; peak = the latest point at or before it whose
equity set/equalled the running peak (None when the peak is still the initial balance and no point
has reached it). Both are None when ``max_drawdown_abs == 0``.

Sharpe (daily, annualised)
--------------------------
Group the equity points by UTC calendar date and take the LAST equity of each date -> ``E_1..E_D``
(days with no equity point are simply absent: no forward fill). ``E_0 = initial_balance``::

    r_d    = E_d / E_(d-1) - 1                  d = 1..D
    sharpe = mean(r) / std(r, ddof=1) * sqrt(252)

Risk-free rate 0. ``sharpe_return_count = D``. None when ``D < 2``, when ``std <= 1e-12`` (constant
returns up to float noise), or when some ``E_(d-1) <= 0`` (return undefined after a blown account).

Worked example (the main case of ``engine/tests/test_backtest_metrics.py``)
--------------------------------------------------------------------------
initial_balance 10000; trades in exit order: +200, +300, -100, 0, -150, -50 (R: 2, 3, -1, 0, None, -0.5).

* wins 2, losses 3, breakeven 1; win_rate 2/6 = 0.333333; gross_profit 500; gross_loss 300;
  net 200 (2.0 %); final 10200; PF 500/300 = 1.666667; expectancy 200/6 = 33.333333;
  avg_win 250; avg_loss -300/3 = -100; largest_win 300; largest_loss -150; streaks W 2, L 2 (the
  breakeven trade splits L,0,L,L); avg_r (2+3-1+0-0.5)/5 = 0.7.
* Equity: 10000 10080 10200 10150 10180 | 10500 10450 10400 | 10300 10400 10250 | 10180 10200.
  Peak 10500 (day 2 03:00), trough 10180 (day 4 05:00): max_drawdown_abs 320,
  max_drawdown_pct 320/10500*100 = 3.047619 %.
* Daily closes E_0..E_4 = 10000, 10180, 10400, 10250, 10200 -> r = 0.018, 0.0216110020,
  -0.0144230769, -0.0048780488; mean 0.0050774691; std(ddof=1) 0.0175093608;
  sharpe 0.2899859754 * 15.8745078664 = 4.6033846470.

Windows (:func:`summarize_windows`)
-----------------------------------
Over the per-window results (each window runs from its own initial balance; windows WITHOUT trades
are included as ``net_profit = 0`` outcomes and also counted in ``windows_without_trades``)::

    mean_*   = arithmetic mean;  median_* = statistics.median (mean of the two middle values when even)
    pct_profitable = count(net_profit > eps) / count * 100
    worst / best   = min / max by net_profit_pct, ties by net_profit, then the lowest index first

All values None (and ``count == 0``) for an empty input.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol, runtime_checkable

from ..logging_setup import get_logger, is_dev_mode

logger = get_logger(__name__)

BREAKEVEN_EPS = 1e-9
TRADING_DAYS_PER_YEAR = 252
SHARPE_STD_EPS = 1e-12

__all__ = [
    "BREAKEVEN_EPS",
    "SHARPE_STD_EPS",
    "TRADING_DAYS_PER_YEAR",
    "BacktestMetrics",
    "EquityPointLike",
    "TradeLike",
    "WindowDistribution",
    "WindowRef",
    "WindowSummaryLike",
    "compute_metrics",
    "summarize_windows",
]


# --------------------------------------------------------------------------------------------------
# Input protocols
# --------------------------------------------------------------------------------------------------


@runtime_checkable
class TradeLike(Protocol):
    net_pnl: float
    r_multiple: float | None
    exit_time: datetime


@runtime_checkable
class EquityPointLike(Protocol):
    time: datetime
    equity: float


@runtime_checkable
class WindowSummaryLike(Protocol):
    index: int
    net_profit: float
    net_profit_pct: float
    trade_count: int


# --------------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _to_utc(value).isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass(frozen=True)
class BacktestMetrics:
    """Metrics of one backtest run. See the module docstring for every formula."""

    initial_balance: float
    final_balance: float
    trade_count: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: float | None
    gross_profit: float
    gross_loss: float
    net_profit: float
    net_profit_pct: float
    profit_factor: float | None
    profit_factor_infinite: bool
    expectancy: float | None
    avg_win: float | None
    avg_loss: float | None
    largest_win: float | None
    largest_loss: float | None
    max_consecutive_wins: int
    max_consecutive_losses: int
    avg_r: float | None
    r_count: int
    max_drawdown_abs: float
    max_drawdown_pct: float
    max_drawdown_peak_time: datetime | None
    max_drawdown_trough_time: datetime | None
    sharpe: float | None
    sharpe_return_count: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict: non-finite floats -> None, datetimes -> UTC ISO-8601 strings."""
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class WindowRef:
    index: int
    net_profit: float
    net_profit_pct: float
    trade_count: int


@dataclass(frozen=True)
class WindowDistribution:
    """Distribution of results over several (e.g. randomly sampled) backtest windows."""

    count: int
    windows_without_trades: int
    mean_net_profit: float | None
    median_net_profit: float | None
    mean_net_profit_pct: float | None
    median_net_profit_pct: float | None
    pct_profitable: float | None
    worst_window: WindowRef | None
    best_window: WindowRef | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (non-finite floats -> None)."""
        return _json_safe(asdict(self))


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------


def _to_utc(t: datetime) -> datetime:
    if not isinstance(t, datetime):
        raise TypeError(f"expected datetime, got {type(t).__name__}")
    if t.tzinfo is None or t.utcoffset() is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _finite(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc
    if not math.isfinite(x):
        raise ValueError(f"{what} is not finite: {value!r}")
    return x


def _max_streaks(pnls: Sequence[float]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for pnl in pnls:
        if pnl > BREAKEVEN_EPS:
            cur_w += 1
            cur_l = 0
        elif pnl < -BREAKEVEN_EPS:
            cur_l += 1
            cur_w = 0
        else:  # breakeven ends both runs
            cur_w = cur_l = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def _drawdown(
    points: Sequence[tuple[datetime, float]], initial_balance: float
) -> tuple[float, float, datetime | None, datetime | None]:
    peak = initial_balance
    peak_time: datetime | None = None
    max_abs = 0.0
    max_pct = 0.0
    mdd_peak_time: datetime | None = None
    mdd_trough_time: datetime | None = None
    for t, equity in points:
        if equity >= peak:
            peak = equity
            peak_time = t
            continue
        dd = peak - equity
        if dd > max_abs:
            max_abs = dd
            mdd_peak_time = peak_time
            mdd_trough_time = t
        pct = dd / peak * 100.0
        if pct > max_pct:
            max_pct = pct
    return max_abs, max_pct, mdd_peak_time, mdd_trough_time


def _daily_closes(points: Sequence[tuple[datetime, float]]) -> list[tuple[date, float]]:
    closes: list[tuple[date, float]] = []
    for t, equity in points:
        d = t.date()
        if closes and closes[-1][0] == d:
            closes[-1] = (d, equity)
        else:
            closes.append((d, equity))
    return closes


def _sharpe(closes: Sequence[float], initial_balance: float) -> tuple[float | None, int]:
    series = [initial_balance, *closes]
    returns: list[float] = []
    for prev, cur in zip(series, series[1:]):
        if prev <= 0:
            return None, len(closes)
        returns.append(cur / prev - 1.0)
    n = len(returns)
    if n < 2:
        return None, n
    mean = math.fsum(returns) / n
    var = math.fsum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std <= SHARPE_STD_EPS:
        return None, n
    return mean / std * math.sqrt(TRADING_DAYS_PER_YEAR), n


# --------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------


def compute_metrics(
    trades: Sequence[TradeLike],
    equity: Sequence[EquityPointLike],
    initial_balance: float,
) -> BacktestMetrics:
    """Compute :class:`BacktestMetrics` from closed trades and the mark-to-market equity series."""
    initial = _finite(initial_balance, "initial_balance")
    if initial <= 0:
        raise ValueError(f"initial_balance must be > 0, got {initial_balance!r}")

    # Trades in exit-time order (stable).
    keyed = [(_to_utc(tr.exit_time), i, _finite(tr.net_pnl, f"trades[{i}].net_pnl"), tr.r_multiple)
             for i, tr in enumerate(trades)]
    keyed.sort(key=lambda k: (k[0], k[1]))
    pnls = [k[2] for k in keyed]

    wins = [p for p in pnls if p > BREAKEVEN_EPS]
    losses = [p for p in pnls if p < -BREAKEVEN_EPS]
    n = len(pnls)
    breakeven = n - len(wins) - len(losses)

    gross_profit = math.fsum(wins)
    loss_sum = math.fsum(losses)  # <= 0
    gross_loss = abs(loss_sum)
    net_profit = math.fsum(pnls)

    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    else:
        profit_factor = None
    pf_infinite = gross_profit > 0 and not losses

    r_values: list[float] = []
    for _, _, _, r in keyed:
        if r is None:
            continue
        try:
            rf = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rf):
            r_values.append(rf)

    max_w, max_l = _max_streaks(pnls)

    # Equity series: validate and normalise to UTC.
    points: list[tuple[datetime, float]] = []
    for i, p in enumerate(equity):
        t = _to_utc(p.time)
        if points and t < points[-1][0]:
            raise ValueError(f"equity series is not chronological at index {i}: {t} < {points[-1][0]}")
        points.append((t, _finite(p.equity, f"equity[{i}].equity")))

    dd_abs, dd_pct, dd_peak_t, dd_trough_t = _drawdown(points, initial)
    closes = _daily_closes(points)
    sharpe, sharpe_n = _sharpe([e for _, e in closes], initial)

    metrics = BacktestMetrics(
        initial_balance=initial,
        final_balance=initial + net_profit,
        trade_count=n,
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=breakeven,
        win_rate=(len(wins) / n) if n else None,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        net_profit_pct=net_profit / initial * 100.0,
        profit_factor=profit_factor,
        profit_factor_infinite=pf_infinite,
        expectancy=(net_profit / n) if n else None,
        avg_win=(gross_profit / len(wins)) if wins else None,
        avg_loss=(loss_sum / len(losses)) if losses else None,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        max_consecutive_wins=max_w,
        max_consecutive_losses=max_l,
        avg_r=(math.fsum(r_values) / len(r_values)) if r_values else None,
        r_count=len(r_values),
        max_drawdown_abs=dd_abs,
        max_drawdown_pct=dd_pct,
        max_drawdown_peak_time=dd_peak_t,
        max_drawdown_trough_time=dd_trough_t,
        sharpe=sharpe,
        sharpe_return_count=sharpe_n,
    )

    if is_dev_mode():
        logger.debug(
            "compute_metrics: trades=%d equity_points=%d days=%d initial=%.2f -> %s",
            n, len(points), len(closes), initial, metrics.to_dict(),
        )
    return metrics


def summarize_windows(results: Sequence[WindowSummaryLike]) -> WindowDistribution:
    """Distribution (mean/median/worst/best/%profitable) over per-window backtest results."""
    refs = [
        WindowRef(
            index=int(w.index),
            net_profit=_finite(w.net_profit, f"windows[{i}].net_profit"),
            net_profit_pct=_finite(w.net_profit_pct, f"windows[{i}].net_profit_pct"),
            trade_count=int(w.trade_count),
        )
        for i, w in enumerate(results)
    ]
    count = len(refs)
    if count == 0:
        dist = WindowDistribution(
            count=0, windows_without_trades=0,
            mean_net_profit=None, median_net_profit=None,
            mean_net_profit_pct=None, median_net_profit_pct=None,
            pct_profitable=None, worst_window=None, best_window=None,
        )
    else:
        profits = [r.net_profit for r in refs]
        pcts = [r.net_profit_pct for r in refs]
        worst = min(refs, key=lambda r: (r.net_profit_pct, r.net_profit, r.index))
        best = min(refs, key=lambda r: (-r.net_profit_pct, -r.net_profit, r.index))
        dist = WindowDistribution(
            count=count,
            windows_without_trades=sum(1 for r in refs if r.trade_count == 0),
            mean_net_profit=math.fsum(profits) / count,
            median_net_profit=float(statistics.median(profits)),
            mean_net_profit_pct=math.fsum(pcts) / count,
            median_net_profit_pct=float(statistics.median(pcts)),
            pct_profitable=sum(1 for p in profits if p > BREAKEVEN_EPS) / count * 100.0,
            worst_window=worst,
            best_window=best,
        )

    if is_dev_mode():
        logger.debug("summarize_windows: windows=%d -> %s", count, dist.to_dict())
    return dist
