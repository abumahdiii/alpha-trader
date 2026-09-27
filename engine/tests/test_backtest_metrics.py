"""Backtest metrics (phase 4 section 2, HIGH-RISK) on hand-built trades and equity series.

Every expected value is a literal computed BY HAND (worked out in the comments), never by calling the
formula again. Main case = the worked example in ``alpha_engine/backtest/metrics.py``.
"""

from __future__ import annotations

import io
import json
import logging
import math
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone

import pytest

from alpha_engine.backtest.metrics import (
    BacktestMetrics,
    EquityPointLike,
    TradeLike,
    WindowDistribution,
    WindowRef,
    WindowSummaryLike,
    compute_metrics,
    summarize_windows,
)
from alpha_engine.logging_setup import ROOT_LOGGER_NAME, configure_logging

UTC = timezone.utc
TOL = dict(rel=1e-12, abs=1e-12)


@dataclass(frozen=True)
class T:
    net_pnl: float
    r_multiple: float | None
    exit_time: datetime


@dataclass(frozen=True)
class P:
    time: datetime
    equity: float


@dataclass(frozen=True)
class W:
    index: int
    net_profit: float
    net_profit_pct: float
    trade_count: int


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026-01-<day> hh:mm UTC (5 = Monday)."""
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def _walk_floats(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_floats(v)
    elif isinstance(obj, float):
        yield obj


def assert_json_safe(d: dict) -> None:
    json.dumps(d, allow_nan=False)  # raises on NaN/inf
    assert all(math.isfinite(x) for x in _walk_floats(d))


# ---------------------------------------------------------------------------------------------
# Main hand-built case: 6 trades, 13 equity points over 4 UTC days
# ---------------------------------------------------------------------------------------------

INITIAL = 10_000.0

# Exit order: W W L B L L. Given here deliberately OUT of exit order (streaks must sort by exit_time).
MAIN_TRADES = (
    T(-150.0, None, at(7, 20)),   # T5 (R missing -> ignored in avg_r)
    T(+200.0, 2.0, at(5, 10)),    # T1
    T(+300.0, 3.0, at(6, 3)),     # T2
    T(-100.0, -1.0, at(6, 16)),   # T3
    T(0.0, 0.0, at(7, 12)),       # T4 breakeven
    T(-50.0, -0.5, at(8, 11)),    # T6
)

MAIN_EQUITY = (
    P(at(5, 1), 10_000.0),
    P(at(5, 6), 10_080.0),
    P(at(5, 10), 10_200.0),  # T1 closed
    P(at(5, 18), 10_150.0),
    P(at(5, 23), 10_180.0),  # day 1 close E1
    P(at(6, 3), 10_500.0),   # T2 closed -> all-time peak
    P(at(6, 9), 10_450.0),
    P(at(6, 16), 10_400.0),  # T3 closed, day 2 close E2
    P(at(7, 8), 10_300.0),
    P(at(7, 12), 10_400.0),  # T4 closed
    P(at(7, 20), 10_250.0),  # T5 closed, day 3 close E3
    P(at(8, 5), 10_180.0),   # deepest point: 10500 - 10180 = 320
    P(at(8, 11), 10_200.0),  # T6 closed, day 4 close E4 == 10000 + 200
)


def test_protocols_accept_plain_dataclasses() -> None:
    assert isinstance(MAIN_TRADES[0], TradeLike)
    assert isinstance(MAIN_EQUITY[0], EquityPointLike)
    assert isinstance(W(0, 0.0, 0.0, 0), WindowSummaryLike)


def test_main_case_trade_metrics() -> None:
    m = compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL)
    assert isinstance(m, BacktestMetrics)
    assert m.trade_count == 6
    assert m.win_count == 2
    assert m.loss_count == 3
    assert m.breakeven_count == 1
    assert m.win_rate == pytest.approx(0.3333333333333333, **TOL)       # 2 / 6
    assert m.gross_profit == pytest.approx(500.0, **TOL)                # 200 + 300
    assert m.gross_loss == pytest.approx(300.0, **TOL)                  # |-100 - 150 - 50|
    assert m.net_profit == pytest.approx(200.0, **TOL)
    assert m.net_profit_pct == pytest.approx(2.0, **TOL)                # 200 / 10000 * 100
    assert m.initial_balance == 10_000.0
    assert m.final_balance == pytest.approx(10_200.0, **TOL)
    assert m.profit_factor == pytest.approx(1.6666666666666667, **TOL)  # 500 / 300
    assert m.profit_factor_infinite is False
    assert m.expectancy == pytest.approx(33.333333333333336, **TOL)     # 200 / 6
    assert m.avg_win == pytest.approx(250.0, **TOL)                     # 500 / 2
    assert m.avg_loss == pytest.approx(-100.0, **TOL)                   # -300 / 3 (negative)
    assert m.largest_win == pytest.approx(300.0, **TOL)
    assert m.largest_loss == pytest.approx(-150.0, **TOL)
    assert m.max_consecutive_wins == 2                                  # T1 T2
    assert m.max_consecutive_losses == 2                                # T5 T6 (T4 breakeven splits T3)
    assert m.avg_r == pytest.approx(0.7, **TOL)                         # (2 + 3 - 1 + 0 - 0.5) / 5
    assert m.r_count == 5


def test_main_case_drawdown() -> None:
    m = compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL)
    # Peak 10500 at day 2 03:00, trough 10180 at day 4 05:00.
    assert m.max_drawdown_abs == pytest.approx(320.0, **TOL)
    assert m.max_drawdown_pct == pytest.approx(3.0476190476190474, **TOL)  # 320 / 10500 * 100
    assert m.max_drawdown_peak_time == at(6, 3)
    assert m.max_drawdown_trough_time == at(8, 5)


def test_main_case_sharpe() -> None:
    # E0..E4 = 10000, 10180, 10400, 10250, 10200 (last point of each UTC day)
    # r = 9/500 = 0.018, 11/509 = 0.021611001964636542, -3/208 = -0.014423076923076923,
    #     -1/205 = -0.004878048780487805
    # mean = 0.020309876261071814 / 4 = 0.0050774690652679536
    # squared deviations: 1.669918057591067e-4, 2.733577101345035e-4, 3.802712938435535e-4,
    #                     9.911233557716138e-5 -> sum 9.19733145314325e-4
    # var (ddof=1) = 3.06577715104775e-4 -> std = 0.017509360785156465
    # sharpe = 0.0050774690652679536 / 0.017509360785156465 * sqrt(252)
    #        = 0.28998597536309667 * 15.874507866387544 = 4.603384647043542
    m = compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL)
    assert m.sharpe_return_count == 4
    assert m.sharpe == pytest.approx(4.603384647043542, rel=1e-12)


def test_main_case_to_dict_is_json_safe_and_complete() -> None:
    d = compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL).to_dict()
    assert_json_safe(d)
    assert d["max_drawdown_peak_time"] == "2026-01-06T03:00:00+00:00"
    assert d["max_drawdown_trough_time"] == "2026-01-08T05:00:00+00:00"
    assert d["profit_factor_infinite"] is False
    assert set(d) >= {
        "net_profit", "max_drawdown_abs", "max_drawdown_pct", "win_rate", "profit_factor",
        "expectancy", "sharpe", "trade_count", "avg_r",
    }


def test_deterministic_and_inputs_untouched() -> None:
    trades = list(MAIN_TRADES)
    equity = list(MAIN_EQUITY)
    a = compute_metrics(trades, equity, INITIAL)
    b = compute_metrics(trades, equity, INITIAL)
    assert a == b
    assert trades == list(MAIN_TRADES) and equity == list(MAIN_EQUITY)
    with pytest.raises(FrozenInstanceError):
        a.net_profit = 0.0  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Drawdown edge cases
# ---------------------------------------------------------------------------------------------


def test_drawdown_abs_and_pct_from_different_points() -> None:
    equity = (
        P(at(5, 0), 1_000.0),
        P(at(5, 1), 900.0),     # dd 100 from peak 1000 -> 10 %      (max PCT)
        P(at(5, 2), 3_000.0),
        P(at(5, 3), 2_800.0),   # dd 200 from peak 3000 -> 6.667 %   (max ABS)
    )
    m = compute_metrics((), equity, 1_000.0)
    assert m.max_drawdown_abs == pytest.approx(200.0, **TOL)
    assert m.max_drawdown_pct == pytest.approx(10.0, **TOL)
    assert m.max_drawdown_peak_time == at(5, 2)
    assert m.max_drawdown_trough_time == at(5, 3)


def test_drawdown_peak_is_initial_balance_when_never_reached() -> None:
    m = compute_metrics((), (P(at(5, 1), 950.0), P(at(5, 2), 980.0)), 1_000.0)
    assert m.max_drawdown_abs == pytest.approx(50.0, **TOL)     # 1000 - 950
    assert m.max_drawdown_pct == pytest.approx(5.0, **TOL)      # 50 / 1000 * 100
    assert m.max_drawdown_peak_time is None
    assert m.max_drawdown_trough_time == at(5, 1)


def test_drawdown_zero_when_equity_only_rises() -> None:
    m = compute_metrics((), (P(at(5, 1), 1_000.0), P(at(5, 2), 1_010.0)), 1_000.0)
    assert m.max_drawdown_abs == 0.0
    assert m.max_drawdown_pct == 0.0
    assert m.max_drawdown_peak_time is None and m.max_drawdown_trough_time is None


# ---------------------------------------------------------------------------------------------
# Boundary cases
# ---------------------------------------------------------------------------------------------


def test_no_trades_no_equity() -> None:
    m = compute_metrics((), (), 5_000.0)
    assert m.trade_count == m.win_count == m.loss_count == m.breakeven_count == 0
    assert m.win_rate is None
    assert m.gross_profit == 0.0 and m.gross_loss == 0.0
    assert m.net_profit == 0.0 and m.net_profit_pct == 0.0
    assert m.final_balance == 5_000.0
    assert m.profit_factor is None and m.profit_factor_infinite is False
    assert m.expectancy is None
    assert m.avg_win is None and m.avg_loss is None
    assert m.largest_win is None and m.largest_loss is None
    assert m.max_consecutive_wins == 0 and m.max_consecutive_losses == 0
    assert m.avg_r is None and m.r_count == 0
    assert m.max_drawdown_abs == 0.0 and m.max_drawdown_pct == 0.0
    assert m.max_drawdown_peak_time is None and m.max_drawdown_trough_time is None
    assert m.sharpe is None and m.sharpe_return_count == 0
    assert_json_safe(m.to_dict())


def test_only_wins_profit_factor_none_and_infinite_flag() -> None:
    trades = (T(100.0, 1.0, at(5, 1)), T(50.0, 0.5, at(5, 2)))
    m = compute_metrics(trades, (), 1_000.0)
    assert m.win_count == 2 and m.loss_count == 0
    assert m.win_rate == pytest.approx(1.0, **TOL)
    assert m.gross_loss == 0.0
    assert m.profit_factor is None
    assert m.profit_factor_infinite is True
    assert m.avg_win == pytest.approx(75.0, **TOL)              # 150 / 2
    assert m.avg_loss is None and m.largest_loss is None
    assert m.largest_win == pytest.approx(100.0, **TOL)
    assert m.max_consecutive_wins == 2 and m.max_consecutive_losses == 0
    assert m.expectancy == pytest.approx(75.0, **TOL)
    d = m.to_dict()
    assert_json_safe(d)
    assert d["profit_factor"] is None and d["profit_factor_infinite"] is True


def test_only_losses() -> None:
    trades = (T(-100.0, -1.0, at(5, 1)), T(-50.0, -0.5, at(5, 2)))
    m = compute_metrics(trades, (), 1_000.0)
    assert m.win_count == 0 and m.loss_count == 2
    assert m.win_rate == 0.0
    assert m.gross_profit == 0.0
    assert m.gross_loss == pytest.approx(150.0, **TOL)
    assert m.profit_factor == 0.0                               # 0 / 150
    assert m.profit_factor_infinite is False
    assert m.avg_win is None and m.largest_win is None
    assert m.avg_loss == pytest.approx(-75.0, **TOL)
    assert m.largest_loss == pytest.approx(-100.0, **TOL)
    assert m.max_consecutive_losses == 2 and m.max_consecutive_wins == 0
    assert m.net_profit_pct == pytest.approx(-15.0, **TOL)      # -150 / 1000 * 100
    assert m.final_balance == pytest.approx(850.0, **TOL)
    assert m.avg_r == pytest.approx(-0.75, **TOL)


def test_only_breakeven_trades() -> None:
    trades = (T(0.0, 0.0, at(5, 1)), T(1e-12, None, at(5, 2)), T(-1e-12, None, at(5, 3)))
    m = compute_metrics(trades, (), 1_000.0)
    assert m.breakeven_count == 3 and m.win_count == 0 and m.loss_count == 0
    assert m.profit_factor is None and m.profit_factor_infinite is False
    assert m.max_consecutive_wins == 0 and m.max_consecutive_losses == 0


def test_breakeven_eps_boundary() -> None:
    trades = (T(2e-9, None, at(5, 1)), T(-2e-9, None, at(5, 2)), T(5e-10, None, at(5, 3)))
    m = compute_metrics(trades, (), 1_000.0)
    assert (m.win_count, m.loss_count, m.breakeven_count) == (1, 1, 1)


def test_sharpe_none_with_single_day() -> None:
    equity = (P(at(5, 1), 1_000.0), P(at(5, 12), 1_020.0), P(at(5, 23), 1_010.0))
    m = compute_metrics((), equity, 1_000.0)
    assert m.sharpe_return_count == 1   # only r1 = 1010 / 1000 - 1
    assert m.sharpe is None


def test_sharpe_none_when_std_zero_flat() -> None:
    equity = (P(at(5, 12), 1_000.0), P(at(6, 12), 1_000.0), P(at(7, 12), 1_000.0))
    m = compute_metrics((), equity, 1_000.0)
    assert m.sharpe_return_count == 3
    assert m.sharpe is None


def test_sharpe_none_when_returns_constant() -> None:
    # 1100 / 1000 - 1 == 1210 / 1100 - 1 == 0.1 -> std 0
    equity = (P(at(5, 12), 1_100.0), P(at(6, 12), 1_210.0))
    m = compute_metrics((), equity, 1_000.0)
    assert m.sharpe_return_count == 2
    assert m.sharpe is None


def test_sharpe_groups_by_utc_date_and_skips_missing_days() -> None:
    # 2026-01-06 02:00 at UTC+3 is 2026-01-05 23:00 UTC -> it is the day-5 close, not day 6.
    # Day 6 missing entirely (weekend-like gap) is NOT forward filled.
    plus3 = timezone(timedelta(hours=3))
    equity = (
        P(at(5, 10), 1_010.0),
        P(datetime(2026, 1, 6, 2, 0, tzinfo=plus3), 1_020.0),
        P(at(7, 12), 1_071.0),
    )
    # E0..E2 = 1000, 1020, 1071 -> r = 0.02, 0.05; mean 0.035; std = 0.03/sqrt(2) = 0.021213203435596427
    # sharpe = 0.035 / 0.021213203435596427 * sqrt(252) = 7*sqrt(504)/6 = 26.19160170741759
    m = compute_metrics((), equity, 1_000.0)
    assert m.sharpe_return_count == 2
    assert m.sharpe == pytest.approx(26.19160170741759, rel=1e-12)


def test_naive_datetimes_are_utc() -> None:
    equity = (P(datetime(2026, 1, 5, 1), 1_000.0), P(datetime(2026, 1, 5, 2), 900.0))
    m = compute_metrics((), equity, 1_000.0)
    assert m.max_drawdown_trough_time == at(5, 2)
    assert m.to_dict()["max_drawdown_trough_time"] == "2026-01-05T02:00:00+00:00"


def test_r_multiple_none_and_non_finite_ignored() -> None:
    trades = (
        T(10.0, 1.0, at(5, 1)),
        T(10.0, None, at(5, 2)),
        T(-5.0, float("nan"), at(5, 3)),
        T(-5.0, float("inf"), at(5, 4)),
        T(-5.0, -0.5, at(5, 5)),
    )
    m = compute_metrics(trades, (), 1_000.0)
    assert m.r_count == 2
    assert m.avg_r == pytest.approx(0.25, **TOL)   # (1.0 - 0.5) / 2
    assert_json_safe(m.to_dict())


def test_all_r_missing_gives_none() -> None:
    m = compute_metrics((T(10.0, None, at(5, 1)),), (), 1_000.0)
    assert m.avg_r is None and m.r_count == 0


def test_blown_account_sharpe_none_no_nan() -> None:
    equity = (P(at(5, 12), 0.0), P(at(6, 12), 0.0), P(at(7, 12), 0.0))
    m = compute_metrics((T(-1_000.0, -1.0, at(5, 12)),), equity, 1_000.0)
    assert m.sharpe is None
    assert m.max_drawdown_abs == pytest.approx(1_000.0, **TOL)
    assert m.max_drawdown_pct == pytest.approx(100.0, **TOL)
    assert_json_safe(m.to_dict())


def test_nan_never_appears_in_to_dict_across_cases() -> None:
    cases = [
        compute_metrics((), (), 1_000.0),
        compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL),
        compute_metrics((T(5.0, None, at(5, 1)),), (P(at(5, 1), 1_005.0),), 1_000.0),
        compute_metrics((T(-5.0, float("nan"), at(5, 1)),), (P(at(5, 1), 995.0),), 1_000.0),
    ]
    for m in cases:
        assert_json_safe(m.to_dict())


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_initial_balance_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        compute_metrics((), (), bad)


def test_non_finite_pnl_or_equity_rejected() -> None:
    with pytest.raises(ValueError):
        compute_metrics((T(float("nan"), None, at(5, 1)),), (), 1_000.0)
    with pytest.raises(ValueError):
        compute_metrics((), (P(at(5, 1), float("inf")),), 1_000.0)


def test_unordered_equity_rejected() -> None:
    with pytest.raises(ValueError):
        compute_metrics((), (P(at(5, 2), 1_000.0), P(at(5, 1), 1_000.0)), 1_000.0)


# ---------------------------------------------------------------------------------------------
# Window distribution
# ---------------------------------------------------------------------------------------------

WINDOWS = (
    W(0, 150.0, 1.5, 4),
    W(1, -300.0, -3.0, 6),
    W(2, 0.0, 0.0, 0),      # no trades
    W(3, 420.0, 4.2, 5),
    W(4, -20.0, -0.2, 1),
)


def test_summarize_windows_five() -> None:
    d = summarize_windows(WINDOWS)
    assert isinstance(d, WindowDistribution)
    assert d.count == 5
    assert d.windows_without_trades == 1
    assert d.mean_net_profit == pytest.approx(50.0, **TOL)          # 250 / 5
    assert d.median_net_profit == pytest.approx(0.0, **TOL)         # -300 -20 [0] 150 420
    assert d.mean_net_profit_pct == pytest.approx(0.5, **TOL)       # 2.5 / 5
    assert d.median_net_profit_pct == pytest.approx(0.0, **TOL)
    assert d.pct_profitable == pytest.approx(40.0, **TOL)           # 2 of 5 (0 is not profitable)
    assert d.worst_window == WindowRef(1, -300.0, -3.0, 6)
    assert d.best_window == WindowRef(3, 420.0, 4.2, 5)
    out = d.to_dict()
    assert_json_safe(out)
    assert out["worst_window"] == {"index": 1, "net_profit": -300.0, "net_profit_pct": -3.0, "trade_count": 6}


def test_summarize_windows_even_count_median() -> None:
    d = summarize_windows(WINDOWS[:4])
    assert d.count == 4
    assert d.median_net_profit == pytest.approx(75.0, **TOL)        # (0 + 150) / 2
    assert d.median_net_profit_pct == pytest.approx(0.75, **TOL)    # (0 + 1.5) / 2
    assert d.mean_net_profit == pytest.approx(67.5, **TOL)          # 270 / 4
    assert d.pct_profitable == pytest.approx(50.0, **TOL)


def test_summarize_windows_ties_pick_lowest_index() -> None:
    d = summarize_windows((W(7, 10.0, 1.0, 1), W(3, 10.0, 1.0, 1), W(5, 10.0, 1.0, 2)))
    assert d.worst_window is not None and d.worst_window.index == 3
    assert d.best_window is not None and d.best_window.index == 3


def test_summarize_windows_empty() -> None:
    d = summarize_windows(())
    assert d.count == 0 and d.windows_without_trades == 0
    assert d.mean_net_profit is None and d.median_net_profit is None
    assert d.mean_net_profit_pct is None and d.median_net_profit_pct is None
    assert d.pct_profitable is None
    assert d.worst_window is None and d.best_window is None
    assert_json_safe(d.to_dict())


# ---------------------------------------------------------------------------------------------
# DEV_MODE logging (rule 01)
# ---------------------------------------------------------------------------------------------


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.mark.parametrize("dev", [True, False])
def test_debug_log_only_under_dev_mode(make_settings, dev: bool) -> None:
    configure_logging(make_settings(f"DEV_MODE={'true' if dev else 'false'}\n"), stream=io.StringIO(),
                      force=True)
    handler = _ListHandler()
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.addHandler(handler)
    try:
        compute_metrics(MAIN_TRADES, MAIN_EQUITY, INITIAL)
        summarize_windows(WINDOWS)
    finally:
        root.removeHandler(handler)
    metric_msgs = [m for m in handler.messages if m.startswith(("compute_metrics:", "summarize_windows:"))]
    if dev:
        assert len(metric_msgs) == 2
        assert "trades=6 equity_points=13 days=4" in metric_msgs[0]
        assert "windows=5" in metric_msgs[1]
    else:
        assert metric_msgs == []
