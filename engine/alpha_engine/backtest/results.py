"""From a :class:`~alpha_engine.backtest.models.RunResult` to what is stored and served (phase 4, wave 2).

No new financial math lives here: every number comes from :func:`metrics.compute_metrics` /
:func:`metrics.summarize_windows`; this module only decides WHICH inputs they get and how the results of
several windows are put side by side.

Metrics (always computed on the FULL per-bar equity, before any downsampling)
----------------------------------------------------------------------------
* **per window**: ``compute_metrics(window.trades, window.equity, window.initial_balance)`` -- each
  window starts flat with the run's initial balance (``simulator.simulate_window``).
* **manual run** (one window): the overall metrics ARE the metrics of that window
  (``metrics_kind = "single_window"``; the full :class:`~metrics.BacktestMetrics` dict).
* **random run** (N independent, possibly overlapping windows): windows are NOT chained into one equity
  curve (they overlap in time and each restarts from the initial balance). Instead:

  - ``distribution`` = ``summarize_windows(windows)`` (mean/median net profit and %, % profitable windows,
    worst/best window);
  - ``metrics`` (``metrics_kind = "random_aggregate"``) =

    ``pooled``  -- the trade-level fields of ``compute_metrics(all trades of all windows, [], initial)``
                   (count, win/loss/breakeven counts, win_rate, gross profit/loss, profit_factor,
                   expectancy, avg/largest win/loss, avg_r). A trade of a period covered by two
                   overlapping windows is counted once per window: the pool is "all sampled trades".
                   Balance-, streak-, drawdown- and Sharpe-fields are left out (meaningless across
                   overlapping, independently restarted windows);
    ``mean``    -- arithmetic means over the windows of the per-window metrics: net_profit,
                   net_profit_pct, max_drawdown_abs, max_drawdown_pct, trade_count (all windows);
                   win_rate (windows with >= 1 trade; ``win_rate_windows``); sharpe (windows whose
                   Sharpe is defined; ``sharpe_windows``); None when no window qualifies;
    ``worst``   -- the largest per-window max_drawdown_abs / max_drawdown_pct and the smallest
                   net_profit_pct;
    ``total_trades``, ``window_count``, ``windows_with_trades``, ``initial_balance``, ``note_fa``.

Stored equity (downsampling -- storage/display only)
----------------------------------------------------
The simulator produces one mark-to-market point per H1 bar (~31k points for 5 years). What is stored per
window is the subsequence of those points (values unchanged) that are

1. the first and the last point;
2. the last point of every UTC calendar day (exactly the points the daily Sharpe uses);
3. the point at the CLOSE of every trade's exit bar (``exit_bar_time + 1h``: the realized balance
   after each exit);
4. the points at the window's max-drawdown peak and trough time (``BacktestMetrics.max_drawdown_*_time``),
   so the stored curve shows the true maximum drawdown.

Consequences: recomputing Sharpe from the stored points gives exactly the stored Sharpe (rule 2), and the
max absolute drawdown of the stored points equals the stored ``max_drawdown_abs`` (rule 4); intrabar-free
intraday wiggles between those points are dropped. The metrics themselves are never recomputed from the
stored curve.

Worked example (one day; first point 00:00 = 1000 (initial), H1 closes 01:00..05:00 with equity 1010,
990, 1005, 1000, 1020; one trade exits in the bar that closes at 03:00): max drawdown = peak 1010 (01:00)
- trough 990 (02:00) = 20. Stored = 00:00 (first), 01:00 (peak), 02:00 (trough), 03:00 (exit), 05:00
(last of the day and last point) -> 5 of 6 points; only 04:00 (1000) is dropped, and the stored points
still give max drawdown 1010 - 990 = 20 (tests/test_backtest_results.py).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from ..logging_setup import get_logger, is_dev_mode
from .metrics import BacktestMetrics, compute_metrics, summarize_windows
from .models import EquityPoint, RunResult, Trade, WindowResult

logger = get_logger(__name__)

H1 = timedelta(hours=1)
EQUITY_STORAGE_RULE = "first + last + last point of each UTC day + trade-exit bar closes + max-drawdown peak/trough"
POOLED_FIELDS: tuple[str, ...] = (
    "trade_count", "win_count", "loss_count", "breakeven_count", "win_rate", "gross_profit", "gross_loss",
    "profit_factor", "profit_factor_infinite", "expectancy", "avg_win", "avg_loss", "largest_win",
    "largest_loss", "avg_r", "r_count",
)
RANDOM_NOTE_FA = (
    "پنجره‌های تصادفی مستقل هستند و هر کدام با موجودی اولیه شروع می‌شوند و ممکن است هم‌پوشانی داشته باشند؛ "
    "بنابراین یک منحنی سرمایه پیوسته ساخته نمی‌شود. «تجمیعی» یعنی همه معاملات همه پنجره‌ها روی هم، "
    "و «میانگین» یعنی میانگین معیارهای پنجره‌ها."
)

MetricsKind = Literal["single_window", "random_aggregate"]


@dataclass(frozen=True)
class WindowOutput:
    result: WindowResult
    metrics: BacktestMetrics
    equity_stored: list[EquityPoint]


@dataclass(frozen=True)
class RunOutput:
    result: RunResult
    windows: list[WindowOutput]
    metrics_kind: MetricsKind
    metrics: dict[str, Any]  # JSON-safe
    distribution: dict[str, Any] | None  # JSON-safe, random mode only
    trade_count: int
    net_profit: float  # manual: the window's net profit; random: mean window net profit
    net_profit_pct: float


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def downsample_equity(
    points: Sequence[EquityPoint],
    trades: Sequence[Trade],
    metrics: BacktestMetrics | None = None,
) -> list[EquityPoint]:
    """The stored subsequence of ``points`` (see the module docstring). Values are never altered."""
    n = len(points)
    if n == 0:
        return []
    keep = {0, n - 1}
    days = [_utc(p.time).date() for p in points]
    for i in range(n - 1):
        if days[i] != days[i + 1]:
            keep.add(i)
    marks = {_utc(t.exit_bar_time) + H1 for t in trades}
    if metrics is not None:
        for t in (metrics.max_drawdown_peak_time, metrics.max_drawdown_trough_time):
            if t is not None:
                marks.add(_utc(t))
    if marks:
        for i, p in enumerate(points):
            if _utc(p.time) in marks:
                keep.add(i)
    return [points[i] for i in sorted(keep)]


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def random_aggregate(windows: Sequence[WindowResult], per_window: Sequence[BacktestMetrics],
                     distribution: dict[str, Any]) -> dict[str, Any]:
    """The ``random_aggregate`` metrics block (see the module docstring)."""
    initial = windows[0].initial_balance if windows else None
    trades = [t for w in windows for t in w.trades]
    pooled_full = compute_metrics(trades, [], initial if initial else 1.0).to_dict()
    pooled = {k: pooled_full[k] for k in POOLED_FIELDS}
    with_trades = [m for m in per_window if m.trade_count > 0]
    sharpes = [m.sharpe for m in per_window if m.sharpe is not None]
    block = {
        "window_count": len(per_window),
        "windows_with_trades": len(with_trades),
        "initial_balance": initial,
        "total_trades": len(trades),
        "pooled": pooled,
        "mean": {
            "net_profit": distribution.get("mean_net_profit"),
            "net_profit_pct": distribution.get("mean_net_profit_pct"),
            "max_drawdown_abs": _mean([m.max_drawdown_abs for m in per_window]),
            "max_drawdown_pct": _mean([m.max_drawdown_pct for m in per_window]),
            "trade_count": _mean([float(m.trade_count) for m in per_window]),
            "win_rate": _mean([m.win_rate for m in with_trades if m.win_rate is not None]),
            "win_rate_windows": len(with_trades),
            "sharpe": _mean(sharpes),
            "sharpe_windows": len(sharpes),
        },
        "worst": {
            "max_drawdown_abs": max((m.max_drawdown_abs for m in per_window), default=None),
            "max_drawdown_pct": max((m.max_drawdown_pct for m in per_window), default=None),
            "net_profit_pct": min((m.net_profit_pct for m in per_window), default=None),
        },
        "note_fa": RANDOM_NOTE_FA,
    }
    return block


def build_run_output(result: RunResult) -> RunOutput:
    """Metrics (full equity) + downsampled equity for every window, and the overall block."""
    outputs: list[WindowOutput] = []
    for w in result.windows:
        m = compute_metrics(w.trades, w.equity, w.initial_balance)
        outputs.append(WindowOutput(result=w, metrics=m, equity_stored=downsample_equity(w.equity, w.trades, m)))
    per_window = [o.metrics for o in outputs]
    if result.plan.mode == "manual":
        only = per_window[0]
        kind: MetricsKind = "single_window"
        metrics = only.to_dict()
        distribution = None
        net_profit, net_profit_pct = only.net_profit, only.net_profit_pct
    else:
        kind = "random_aggregate"
        distribution = summarize_windows(result.windows).to_dict()
        metrics = random_aggregate(result.windows, per_window, distribution)
        net_profit = distribution["mean_net_profit"] if distribution["mean_net_profit"] is not None else 0.0
        net_profit_pct = distribution["mean_net_profit_pct"] if distribution["mean_net_profit_pct"] is not None else 0.0
    out = RunOutput(result=result, windows=outputs, metrics_kind=kind, metrics=metrics, distribution=distribution,
                    trade_count=result.total_trades, net_profit=net_profit, net_profit_pct=net_profit_pct)
    if is_dev_mode():
        logger.debug("backtest output %s %s: kind=%s trades=%d net=%.2f (%.4f%%) equity points full=%d stored=%d",
                     result.config.symbol, result.plan.mode, kind, out.trade_count, net_profit, net_profit_pct,
                     sum(len(o.result.equity) for o in outputs), sum(len(o.equity_stored) for o in outputs))
    return out


__all__ = [
    "EQUITY_STORAGE_RULE",
    "POOLED_FIELDS",
    "RANDOM_NOTE_FA",
    "RunOutput",
    "WindowOutput",
    "build_run_output",
    "downsample_equity",
    "random_aggregate",
]
