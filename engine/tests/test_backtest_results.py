"""backtest/results.py: equity downsampling for storage and the random-mode aggregate (hand-made inputs)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.backtest.metrics import compute_metrics, summarize_windows
from alpha_engine.backtest.models import EquityPoint
from alpha_engine.backtest.results import POOLED_FIELDS, downsample_equity, random_aggregate

T0 = datetime(2025, 3, 3, tzinfo=timezone.utc)  # Monday 00:00 UTC
H = timedelta(hours=1)


@dataclass
class T:  # the Trade fields the functions read
    net_pnl: float
    r_multiple: float | None
    exit_time: datetime
    exit_bar_time: datetime


def eq(hours: list[int], values: list[float]) -> list[EquityPoint]:
    return [EquityPoint(time=T0 + h * H, balance=v, equity=v) for h, v in zip(hours, values, strict=True)]


def test_downsample_worked_example() -> None:
    """Docstring example: 00:00 = 1000, closes 01..05 = 1010, 990, 1005, 1000, 1020; exit bar closes 03:00."""
    points = eq([0, 1, 2, 3, 4, 5], [1000, 1010, 990, 1005, 1000, 1020])
    trades = [T(net_pnl=5.0, r_multiple=0.5, exit_time=T0 + 3 * H, exit_bar_time=T0 + 2 * H)]
    m = compute_metrics(trades, points, 1000.0)
    assert (m.max_drawdown_abs, m.max_drawdown_peak_time, m.max_drawdown_trough_time) == (20.0, T0 + H, T0 + 2 * H)
    kept = downsample_equity(points, trades, m)
    assert [p.time for p in kept] == [T0, T0 + H, T0 + 2 * H, T0 + 3 * H, T0 + 5 * H]
    assert compute_metrics(trades, kept, 1000.0).max_drawdown_abs == m.max_drawdown_abs == 20.0
    assert all(p in points for p in kept)  # values never altered


def test_downsample_keeps_daily_closes_so_sharpe_is_unchanged() -> None:
    hours = list(range(0, 24 * 5, 3))  # 5 days, a point every 3 hours
    values = [1000 + 10 * math.sin(h / 7.0) + h * 0.3 for h in hours]
    points = eq(hours, values)
    full = compute_metrics([], points, 1000.0)
    kept = downsample_equity(points, [], full)
    assert len(kept) < len(points) / 3
    again = compute_metrics([], kept, 1000.0)
    assert again.sharpe == full.sharpe and again.sharpe_return_count == full.sharpe_return_count == 5
    assert again.max_drawdown_abs == full.max_drawdown_abs
    assert downsample_equity([], [], None) == []
    one = eq([0], [1000])
    assert downsample_equity(one, [], None) == one


@dataclass
class W:  # WindowResult fields read by random_aggregate / summarize_windows
    index: int
    trades: list[T]
    initial_balance: float = 1000.0

    @property
    def net_profit(self) -> float:
        return math.fsum(t.net_pnl for t in self.trades)

    @property
    def net_profit_pct(self) -> float:
        return self.net_profit / self.initial_balance * 100.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)


def test_random_aggregate_pools_trades_and_averages_window_metrics() -> None:
    """Window 0: +30, -10 (R 3, -1); window 1: -20 (R -2); window 2: no trade.
    pooled: 3 trades, 1 win, win_rate 1/3, gross 30/30 -> PF 1.0, expectancy 0/3 = 0, avg_r 0.
    mean net = (20 - 20 + 0) / 3 = 0; mean win_rate over windows with trades = (0.5 + 0) / 2 = 0.25."""
    def t(pnl: float, r: float, hour: int) -> T:
        return T(net_pnl=pnl, r_multiple=r, exit_time=T0 + hour * H, exit_bar_time=T0 + (hour - 1) * H)

    windows = [W(0, [t(30, 3, 2), t(-10, -1, 5)]), W(1, [t(-20, -2, 3)]), W(2, [])]
    curves = [eq([0, 2, 5], [1000, 1030, 1020]), eq([0, 3], [1000, 980]), eq([0, 4], [1000, 1000])]
    per = [compute_metrics(w.trades, c, 1000.0) for w, c in zip(windows, curves, strict=True)]
    dist = summarize_windows(windows).to_dict()
    agg = random_aggregate(windows, per, dist)
    assert set(agg["pooled"]) == set(POOLED_FIELDS)
    p = agg["pooled"]
    assert (p["trade_count"], p["win_count"], p["loss_count"]) == (3, 1, 2)
    assert p["win_rate"] == pytest.approx(1 / 3) and p["profit_factor"] == 1.0 and p["expectancy"] == 0.0
    assert p["avg_r"] == 0.0 and p["largest_loss"] == -20.0
    assert agg["total_trades"] == 3 and agg["window_count"] == 3 and agg["windows_with_trades"] == 2
    assert agg["mean"]["net_profit"] == 0.0 and agg["mean"]["win_rate"] == 0.25 and agg["mean"]["win_rate_windows"] == 2
    assert agg["mean"]["max_drawdown_abs"] == pytest.approx((10 + 20 + 0) / 3)
    assert agg["worst"]["max_drawdown_abs"] == 20.0 and agg["worst"]["net_profit_pct"] == -2.0
    assert agg["mean"]["trade_count"] == 1.0 and agg["initial_balance"] == 1000.0 and agg["note_fa"]
