"""Independent per-setup outcomes, their summary and the backtest flag (backtest/setup_outcomes.py).

Hand-computed worked examples (gold-like spec: point 0.01, tick 0.01 / 1.00 -> 100 USD per 1.00 price move
per lot, volume 0.01/0.01/100, contract 100; bars are BID prices, spread in points x 0.01), then the
properties that make the chart's setups table trustworthy: same rules as the backtest (equivalence with real
backtest trades), no look-ahead (truncation invariance), independence, no mutation of scan candidates.
"""

from __future__ import annotations

import copy
import math
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from alpha_engine.backtest.costs import resolve_fallback_points
from alpha_engine.backtest.history import HistoryData, build_bar_arrays, scan_full_history
from alpha_engine.backtest.models import CostModel, ExitReason, RunConfig, SkipReason, Window
from alpha_engine.backtest.periods import earliest_start
from alpha_engine.backtest.runner import run_backtest
from alpha_engine.backtest.setup_outcomes import (
    END_OF_DATA_FA,
    OUTSIDE_BACKTEST_WINDOW_FA,
    POSITION_OPEN_IN_BACKTEST_FA,
    backtest_flags,
    backtest_window_for_range,
    counts_only_summary,
    simulate_setup,
    simulate_setups,
    summarize_setups,
)
from alpha_engine.backtest.simulator import scan_provider, simulate_window
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy, resolve_params
from alpha_engine.strategy.params import params_hash
from alpha_engine.strategy.signal import Setup, SignalCandidate
from fixtures.channel_scenarios import random_walk

SPEC = SymbolSpec(name="XAUUSD.x", digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                  currency_base="XAU")
ACCOUNT = AccountSettings(balance=2500.0, risk_pct=1.0, leverage=100, rr=2.0)  # risk 25.00 per setup
NO_COST = CostModel()
COMMISSION = CostModel(commission_per_lot_per_side=3.5)
T0 = pd.Timestamp("2026-01-06 00:00", tz="UTC")  # Tuesday
STRATEGY = StdDevChannelStrategy()
_, CLEAN = resolve_params({})
HASH = params_hash(CLEAN)

CONF_BUY = (2001.00, 2002.00, 1999.00, 2000.50, 25)
CONF_SELL = (1999.00, 2001.00, 1998.50, 2000.50, 30)


def frame(rows: list[tuple], times: list[pd.Timestamp] | None = None) -> pd.DataFrame:
    """rows = (open, high, low, close, spread_points); consecutive hours unless ``times`` is given."""
    times = times or [T0 + pd.Timedelta(hours=i) for i in range(len(rows))]
    return pd.DataFrame({
        "time": pd.DatetimeIndex(times), "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows], "tick_volume": 1,
        "spread": [r[4] for r in rows], "real_volume": 0,
    })


def cand(h1: pd.DataFrame, i: int, direction: str, sl: float, rr: float = 2.0) -> SignalCandidate:
    conf = h1["time"].iloc[i].to_pydatetime()
    return SignalCandidate(
        strategy_name="stddev_channel", strategy_version=1, params_hash="a" * 64, symbol=SPEC.name, direction=direction,
        setup=Setup.BOUNCE_LOWER if direction == "buy" else Setup.BOUNCE_UPPER,
        line="lower" if direction == "buy" else "upper", decision_time_utc=conf + timedelta(hours=1),
        confirmation_bar_open_utc=conf, reference_price=float(h1["close"].iloc[i]), stop_loss=sl, rr=rr,
        pattern="pin_bar", reason_fa="آزمایشی", extra={"line_value": 1999.0},
    )


def outcome(h1: pd.DataFrame, i: int, direction: str, sl: float, *, account: AccountSettings = ACCOUNT,
            costs: CostModel = NO_COST, fallback: int | None = None):
    bars = build_bar_arrays(h1, SPEC, fallback)
    return simulate_setup(bars, cand(h1, i, direction, sl), i, spec=SPEC, account=account, cost_model=costs)


# ---------------------------------------------------------------------------------------------- worked examples
def example_buy_tp():
    """Buy TP. SL 1995.00; entry bar bid open 2000.00, spread 25 pts -> entry = ask = 2000.00 + 0.25 = 2000.25.
    distance 5.25; TP = 2000.25 + 2 * 5.25 = 2010.75; risk 1 % of 2500 = 25.00;
    volume = floor(25 / (5.25 * 100), 0.01) = floor(0.047619) = 0.04; risk_amount = 0.04 * 525 = 21.00.
    bar 2 bid high 2011.00 >= TP -> exit 2010.75 (intrabar -> exit time = bar 2 close);
    pnl_price = 2010.75 - 2000.25 = 10.50; gross = 10.50 * 0.04 * 100 = 42.00; commission 0; net 42.00;
    R = 42.00 / 21.00 = +2.0."""
    h1 = frame([CONF_BUY, (2000.00, 2003.00, 1999.50, 2002.00, 25), (2002.00, 2011.00, 2001.50, 2010.90, 25),
                (2010.00, 2011.00, 2009.00, 2010.50, 25)])
    return h1, outcome(h1, 0, "buy", 1995.00)


def example_sell_sl_on_ask():
    """Sell SL on the ask. SL 2005.00; entry bar bid open 2000.00, spread 30 -> ask open 2000.30 < SL ->
    entry = bid open 2000.00; distance 5.00; TP = 2000.00 - 2 * 5.00 = 1990.00;
    volume = floor(25 / (5.00 * 100)) = 0.05; risk_amount = 0.05 * 500 = 25.00;
    bar 2: bid high 2004.80 < SL but ask high = 2004.80 + 0.30 = 2005.10 >= SL -> SL at 2005.00;
    pnl_price = (2005.00 - 2000.00) * -1 = -5.00; gross = -5.00 * 0.05 * 100 = -25.00;
    commission = 3.50 * 0.05 * 2 = 0.35; net = -25.35; R = -25.35 / 25.00 = -1.014."""
    h1 = frame([CONF_SELL, (2000.00, 2001.00, 1999.00, 2000.20, 30), (2000.20, 2004.80, 1999.50, 2003.00, 30)])
    return h1, outcome(h1, 0, "sell", 2005.00, costs=COMMISSION)


def example_buy_sl_gap():
    """Gap exit. Buy as above (entry 2000.25, SL 1995.00, TP 2010.75, 0.04 lot, risk 21.00);
    bar 2 opens (bid) 1990.00 <= SL -> sl_gap at the open 1990.00 (exit time = bar 2 OPEN);
    pnl_price = 1990.00 - 2000.25 = -10.25; gross = -10.25 * 0.04 * 100 = -41.00; R = -41 / 21 = -1.952381."""
    h1 = frame([CONF_BUY, (2000.00, 2001.00, 1999.00, 2000.00, 25), (1990.00, 1991.00, 1989.00, 1990.50, 25)])
    return h1, outcome(h1, 0, "buy", 1995.00)


def example_sell_end_of_data():
    """End of data. Sell entry 2000.00 (SL 2005.00, TP 1990.00, 0.05 lot, risk 25.00), never hit;
    the LAST cached bar closes (bid) 1999.00 with spread 40 -> short closed at the ask close 1999.40;
    pnl_price = (1999.40 - 2000.00) * -1 = +0.60; gross = 0.60 * 0.05 * 100 = +3.00; R = 3.00 / 25.00 = +0.12."""
    h1 = frame([CONF_SELL, (2000.00, 2001.00, 1999.00, 2000.20, 30), (2000.00, 2001.00, 1998.00, 1999.00, 40)])
    return h1, outcome(h1, 0, "sell", 2005.00)


def test_worked_example_buy_take_profit_with_ask_entry() -> None:
    h1, o = example_buy_tp()
    tr = o.trade
    assert o.status == "accepted" and o.result == "tp" and o.rejection is None
    assert tr.entry == pytest.approx(2000.25) and tr.entry_bid_open == 2000.00 and tr.spread_at_entry_points == 25
    assert tr.stop_loss == 1995.00 and tr.take_profit == pytest.approx(2010.75)
    assert tr.volume == 0.04 and tr.risk_amount == pytest.approx(21.00) and tr.balance_before == 2500.0
    assert tr.exit_reason is ExitReason.TP and tr.exit_price == pytest.approx(2010.75)
    assert o.pnl_price == pytest.approx(10.50) and tr.gross_pnl == pytest.approx(42.00)
    assert tr.commission == 0 and tr.net_pnl == pytest.approx(42.00) and tr.r_multiple == pytest.approx(2.0)
    assert tr.exit_bar_time == h1["time"].iloc[2].to_pydatetime() and o.exit_index == 2
    assert tr.exit_time == (h1["time"].iloc[2] + pd.Timedelta(hours=1)).to_pydatetime()
    assert tr.bars_held == 2 and o.position.sizing.margin == pytest.approx(0.04 * 100 * 2000.25 / 100)


def test_worked_example_sell_stop_loss_on_the_ask() -> None:
    _, o = example_sell_sl_on_ask()
    tr = o.trade
    assert o.result == "sl" and tr.exit_reason is ExitReason.SL
    assert tr.entry == 2000.00 and tr.take_profit == pytest.approx(1990.00) and tr.volume == 0.05
    assert tr.risk_amount == pytest.approx(25.00) and tr.exit_price == 2005.00
    assert o.pnl_price == pytest.approx(-5.00) and tr.gross_pnl == pytest.approx(-25.00)
    assert tr.commission == pytest.approx(0.35) and tr.net_pnl == pytest.approx(-25.35)
    assert tr.r_multiple == pytest.approx(-1.014) and tr.spread_at_exit_points == 30


def test_worked_example_gap_exit_at_the_open() -> None:
    h1, o = example_buy_sl_gap()
    tr = o.trade
    assert o.result == "sl" and tr.exit_reason is ExitReason.SL_GAP and tr.exit_price == 1990.00
    assert tr.exit_time == h1["time"].iloc[2].to_pydatetime()  # gap -> known at the bar open
    assert o.pnl_price == pytest.approx(-10.25) and tr.net_pnl == pytest.approx(-41.00)
    assert tr.r_multiple == pytest.approx(-41.0 / 21.0)


def test_worked_example_end_of_data_closes_at_the_last_ask_close() -> None:
    h1, o = example_sell_end_of_data()
    tr = o.trade
    assert o.result == "end_of_data" and tr.exit_reason is ExitReason.END_OF_PERIOD
    assert tr.exit_price == pytest.approx(1999.40) and tr.spread_at_exit_points == 40
    assert o.pnl_price == pytest.approx(0.60) and tr.net_pnl == pytest.approx(3.00)
    assert tr.r_multiple == pytest.approx(0.12) and o.exit_index == 2
    assert tr.exit_time == (h1["time"].iloc[2] + pd.Timedelta(hours=1)).to_pydatetime()
    assert "پایان داده" in END_OF_DATA_FA
    # appending bars changes an end-of-data outcome (it is provisional) ...
    longer = pd.concat([h1, frame([(1999.00, 1999.50, 1989.00, 1989.50, 30)],
                                  times=[h1["time"].iloc[-1] + pd.Timedelta(hours=1)])], ignore_index=True)
    later = outcome(longer, 0, "sell", 2005.00)
    assert later.result == "tp" and later.trade.exit_price == pytest.approx(1990.00)
    # ... while the entry, levels and volume stay the same
    assert (later.trade.entry, later.trade.stop_loss, later.trade.take_profit, later.trade.volume) == (
        tr.entry, tr.stop_loss, tr.take_profit, tr.volume)


def test_summary_worked_example() -> None:
    """Rows: +42.00 (R 2), -25.35 (R -1.014), -41.00 (R -1.952381), end of data +3.00 (R 0.12), a rejection
    and a pending entry.
    total 6, accepted 4, rejected 1, pending 1; closed 3 -> wins 1, losses 2, breakeven 0, win_rate 1/3;
    net = 42 - 25.35 - 41 = -24.35; gross_profit 42.00; gross_loss 25.35 + 41 = 66.35;
    PF = 42 / 66.35 = 0.633007; total_r = 2 - 1.014 - 1.952381 = -0.966381; avg_r = -0.322127;
    the end-of-data row only in open_end_of_data = 1 and open_net_pnl = +3.00."""
    rows = [example_buy_tp()[1], example_sell_sl_on_ask()[1], example_buy_sl_gap()[1], example_sell_end_of_data()[1]]
    h1 = frame([CONF_BUY, (1994.00, 1996.00, 1993.00, 1995.50, 25)])
    rows.append(outcome(h1, 0, "buy", 1995.00))  # gap through the stop -> rejected
    rows.append(outcome(frame([CONF_BUY]), 0, "buy", 1995.00))  # no bar t+1 -> pending
    s = summarize_setups(rows, ACCOUNT.balance)
    assert (s["total"], s["accepted"], s["rejected"], s["pending_entry"]) == (6, 4, 1, 1)
    assert (s["closed"], s["wins"], s["losses"], s["breakeven"], s["open_end_of_data"]) == (3, 1, 2, 0, 1)
    assert s["win_rate"] == pytest.approx(1 / 3)
    assert s["net_pnl"] == pytest.approx(-24.35) and s["gross_profit"] == pytest.approx(42.0)
    assert s["gross_loss"] == pytest.approx(66.35) and s["profit_factor"] == pytest.approx(42 / 66.35)
    assert s["profit_factor"] == pytest.approx(0.633007, abs=1e-6) and not s["profit_factor_infinite"]
    assert s["total_r"] == pytest.approx(2 - 1.014 - 41 / 21) == pytest.approx(-0.966381, abs=1e-6)
    assert s["avg_r"] == pytest.approx((2 - 1.014 - 41 / 21) / 3) and s["r_count"] == 3
    assert s["open_net_pnl"] == pytest.approx(3.00)


def test_summary_edge_cases() -> None:
    empty = summarize_setups([], ACCOUNT.balance)
    assert empty["total"] == 0 and empty["win_rate"] is None and empty["profit_factor"] is None
    assert empty["net_pnl"] == 0 and empty["total_r"] == 0 and empty["avg_r"] is None
    only_wins = summarize_setups([example_buy_tp()[1]], ACCOUNT.balance)
    assert only_wins["profit_factor"] is None and only_wins["profit_factor_infinite"] is True
    assert only_wins["win_rate"] == 1.0
    only_open = summarize_setups([example_sell_end_of_data()[1]], ACCOUNT.balance)
    assert only_open["closed"] == 0 and only_open["win_rate"] is None and only_open["open_net_pnl"] == pytest.approx(3.0)
    counts = counts_only_summary(["accepted", "rejected", "accepted", "pending_entry"])
    assert (counts["total"], counts["accepted"], counts["rejected"], counts["pending_entry"]) == (4, 2, 1, 1)
    assert counts["win_rate"] is None and counts["closed"] == 0


# ---------------------------------------------------------------------------------------------- status rules
def test_statuses_follow_the_simulator_rules() -> None:
    # sell: bid open 2004.90 < SL but ask open 2005.20 >= SL -> gap through the stop (ask side)
    o = outcome(frame([CONF_SELL, (2004.90, 2006.0, 2004.0, 2005.5, 30)]), 0, "sell", 2005.00)
    assert o.status == "rejected" and o.rejection.reason is SkipReason.GAP_THROUGH_STOP
    assert o.rejection.fill == 2004.90 and o.rejection.bid_open == 2004.90 and "حد ضرر" in o.rejection.reason_fa
    # buy: bid open at the SL -> rejected even though the ask is above
    o = outcome(frame([CONF_BUY, (1995.00, 1997.00, 1994.50, 1996.00, 25)]), 0, "buy", 1995.00)
    assert o.status == "rejected" and o.rejection.reason is SkipReason.GAP_THROUGH_STOP
    assert o.rejection.fill == pytest.approx(1995.25)
    # missing gap between t and t+1 (a one-off hole on a Tuesday)
    times = [T0 + pd.Timedelta(hours=h) for h in (0, 1, 2, 4, 5)]
    o = outcome(frame([CONF_BUY] * 3 + [(2000.0, 2001.0, 1999.0, 2000.0, 25)] * 2, times=times), 2, "buy", 1995.00)
    assert o.status == "rejected" and o.rejection.reason is SkipReason.MISSING_GAP and o.rejection.fill is None
    # volume below the minimum: 1 % of 100 = 1.00 / 525 = 0.0019 -> 0.00 < 0.01
    o = outcome(frame([CONF_BUY, (2000.00, 2001.00, 1999.00, 2000.00, 25)]), 0, "buy", 1995.00,
                account=AccountSettings(balance=100.0, risk_pct=1.0, leverage=100, rr=2.0))
    assert o.status == "rejected" and o.rejection.reason is SkipReason.SIZING_REJECTED
    assert "کمتر از حداقل حجم" in o.rejection.reason_fa and o.rejection.take_profit == pytest.approx(2010.75)
    # margin above the balance: leverage 1 -> 0.04 * 100 * 2000.25 = 8001 > 2500
    o = outcome(frame([CONF_BUY, (2000.00, 2001.00, 1999.00, 2000.00, 25)]), 0, "buy", 1995.00,
                account=AccountSettings(balance=2500.0, risk_pct=1.0, leverage=1, rr=2.0))
    assert o.status == "rejected" and "مارجین" in o.rejection.reason_fa
    # no bar t+1 yet
    o = outcome(frame([CONF_BUY]), 0, "buy", 1995.00)
    assert o.status == "pending_entry" and o.rejection.reason is SkipReason.ENTRY_OUTSIDE_WINDOW


def test_entry_uses_the_fallback_spread_and_weekend_entry() -> None:
    """No broker spread yet: fallback 34 pts -> ask = 2000.00 + 0.34; after a weekend the entry is Sunday's open."""
    h1 = frame([(2001, 2002, 1999, 2000.5, 0), (2000.00, 2001.00, 1999.00, 2000.00, 0),
                (2000.00, 2011.50, 1999.00, 2010.00, 50)])
    o = outcome(h1, 0, "buy", 1995.00, fallback=34)
    assert o.trade.entry == pytest.approx(2000.34) and "entry_spread_fallback" in o.trade.flags
    assert o.position.spread_source == "fallback"
    fri, sun = pd.Timestamp("2026-01-09 21:00", tz="UTC"), pd.Timestamp("2026-01-11 23:00", tz="UTC")
    h1 = frame([CONF_BUY, (2001.0, 2011.0, 2000.5, 2010.0, 25)], times=[fri, sun])
    o = outcome(h1, 0, "buy", 1995.00)
    assert o.trade.entry_time == sun.to_pydatetime() and o.trade.entry == pytest.approx(2001.25)


def test_setups_are_independent_of_each_other() -> None:
    """Two overlapping setups: both evaluated with the settings balance (the backtest skips the second)."""
    h1 = frame([CONF_BUY, (2000.00, 2001.00, 1999.00, 2000.00, 25), (2000.00, 2001.00, 1999.00, 2000.00, 25),
                (2000.00, 2011.00, 1999.50, 2010.00, 25)])
    cands = [cand(h1, 0, "buy", 1995.00), cand(h1, 1, "buy", 1990.00)]
    bars = build_bar_arrays(h1, SPEC)
    outs = simulate_setups(bars, cands, spec=SPEC, account=ACCOUNT, cost_model=NO_COST)
    assert [o.status for o in outs] == ["accepted", "accepted"]
    assert all(o.trade.balance_before == 2500.0 for o in outs)
    # second: entry 2000.25, SL 1990 -> distance 10.25 -> 25 / 1025 = 0.0243 -> 0.02
    assert outs[1].trade.volume == 0.02 and outs[0].trade.volume == 0.04
    window = Window(index=0, start=h1["time"].iloc[0].to_pydatetime(),
                    end=(h1["time"].iloc[-1] + pd.Timedelta(hours=1)).to_pydatetime())
    res = simulate_window(bars, window, scan_provider({0: cands[0], 1: cands[1]}), spec=SPEC, account=ACCOUNT,
                          cost_model=NO_COST)
    assert len(res.trades) == 1 and res.skipped[0].reason is SkipReason.POSITION_OPEN
    flags = backtest_flags(res, cands)
    assert flags[0].traded and flags[0].trade_index == 0 and flags[0].net_pnl == res.trades[0].net_pnl
    assert not flags[1].traded and flags[1].reason == "position_open" and flags[1].reason_fa == POSITION_OPEN_IN_BACKTEST_FA


def test_wrong_confirmation_index_is_refused() -> None:
    h1 = frame([CONF_BUY, (2000.00, 2001.00, 1999.00, 2000.00, 25)])
    with pytest.raises(ValueError):
        simulate_setup(build_bar_arrays(h1, SPEC), cand(h1, 0, "buy", 1995.0), 1, spec=SPEC, account=ACCOUNT,
                       cost_model=NO_COST)


# ---------------------------------------------------------------------------------------------- random walk
@pytest.fixture(scope="module")
def walk() -> HistoryData:
    h1, h4 = random_walk(3000, seed=7)
    h1 = h1.copy()
    rng = np.random.default_rng(3)
    spread = rng.integers(10, 60, len(h1))
    spread[:1200] = 0  # no broker spread before bar 1200 -> fallback
    spread[rng.random(len(h1)) < 0.05] = 0
    h1["spread"] = spread
    return HistoryData(symbol=SPEC.name, h1=h1, h4=h4, spec=SPEC)


@pytest.fixture(scope="module")
def walk_scan(walk: HistoryData):
    return scan_full_history(STRATEGY, walk.h1, walk.h4, CLEAN, ACCOUNT, symbol=walk.symbol)


def _full_config(walk: HistoryData, scan, costs: CostModel) -> RunConfig:
    times = pd.DatetimeIndex(walk.h1["time"]).as_unit("ns").asi8
    earliest = earliest_start(times, pd.DatetimeIndex(walk.h4["time"]).as_unit("ns").asi8, scan.first_valid_index, 14)
    end = pd.Timestamp(int(times[-1]), tz="UTC") + pd.Timedelta(hours=1)
    return RunConfig(symbol=walk.symbol, mode="manual", start=earliest.to_pydatetime(), end=end.to_pydatetime(),
                     cost_model=costs, account=ACCOUNT, strategy_name=STRATEGY.name, strategy_version=STRATEGY.version,
                     params=CLEAN, params_hash=HASH)


def test_outcomes_equal_backtest_trades_with_the_same_balance(walk: HistoryData, walk_scan) -> None:
    """Shared rules: every backtest trade equals the independent outcome of its setup sized with the trade's own
    balance (entry, SL, TP, volume, exit, pnl, R, flags -- field for field), and every non-overlap skip equals
    the outcome's rejection."""
    result = run_backtest(_full_config(walk, walk_scan, COMMISSION), walk, walk_scan)
    win = result.windows[0]
    assert len(win.trades) >= 20 and {t.exit_reason.value for t in win.trades} >= {"sl", "tp"}
    bars = build_bar_arrays(walk.h1, SPEC)
    by_conf = {c.confirmation_bar_open_utc: (t, c) for t, c in walk_scan.by_bar.items()}
    ignore = {"window_index", "trade_index", "balance_before", "balance_after"}
    for tr in win.trades:
        t, c = by_conf[tr.confirmation_bar_time]
        acct = AccountSettings(balance=tr.balance_before, risk_pct=1.0, leverage=100, rr=2.0)
        o = simulate_setup(bars, c, t, spec=SPEC, account=acct, cost_model=COMMISSION)
        assert o.status == "accepted"
        assert o.trade.model_dump(exclude=ignore) == tr.model_dump(exclude=ignore)
        assert o.result == {"tp": "tp", "tp_gap": "tp", "sl": "sl", "sl_gap": "sl",
                            "end_of_period": "end_of_data"}[tr.exit_reason.value]
    rejections = [s for s in win.skipped if s.reason is not SkipReason.POSITION_OPEN]
    for sk in rejections:
        t, c = by_conf[sk.confirmation_bar_time]
        o = simulate_setup(bars, c, t, spec=SPEC, account=ACCOUNT, cost_model=COMMISSION)
        assert o.status in ("rejected", "pending_entry") and o.rejection.reason is sk.reason


def test_non_overlapping_setups_equal_the_backtest_with_the_settings_balance(walk: HistoryData, walk_scan) -> None:
    """Only the setups the backtest traded (never overlapping by construction) and the zero-cost default: the
    independent outcome with the SETTINGS balance equals the trade whenever the backtest balance gives the same
    volume -- here checked on the first trade (balance_before = settings balance)."""
    result = run_backtest(_full_config(walk, walk_scan, NO_COST), walk, walk_scan)
    first = result.windows[0].trades[0]
    assert first.balance_before == ACCOUNT.balance
    bars = build_bar_arrays(walk.h1, SPEC)
    t = next(t for t, c in walk_scan.by_bar.items() if c.confirmation_bar_open_utc == first.confirmation_bar_time)
    o = simulate_setup(bars, walk_scan.by_bar[t], t, spec=SPEC, account=ACCOUNT, cost_model=NO_COST)
    ignore = {"window_index", "trade_index", "balance_after"}
    assert o.trade.model_dump(exclude=ignore) == first.model_dump(exclude=ignore)


def test_truncation_invariance_no_look_ahead(walk: HistoryData, walk_scan) -> None:
    """An outcome that exits at bar e is unchanged when every bar after e is dropped; truncated while still
    open it becomes an end-of-data outcome at the new last bar. (Fixed fallback: the auto value is a
    whole-history cost constant by design, see costs.py.)"""
    raw = walk.h1["spread"].to_numpy()
    fb, _ = resolve_fallback_points(raw, None)
    bars = build_bar_arrays(walk.h1, SPEC, fb)
    cands = walk_scan.candidates
    full = simulate_setups(bars, cands, spec=SPEC, account=ACCOUNT, cost_model=COMMISSION)
    closed = [o for o in full if o.result in ("tp", "sl")]
    assert len(closed) >= 30
    for o in closed[:: max(1, len(closed) // 12)]:
        e = o.exit_index
        cut = build_bar_arrays(walk.h1.iloc[: e + 1].reset_index(drop=True), SPEC, fb)
        again = simulate_setup(cut, o.candidate, o.t, spec=SPEC, account=ACCOUNT, cost_model=COMMISSION)
        assert again.result == o.result and again.exit_index == e
        assert again.trade.model_dump() == o.trade.model_dump()
        if e > o.position.entry_index:  # cut one bar earlier: still open -> end of data at e - 1
            cut = build_bar_arrays(walk.h1.iloc[:e].reset_index(drop=True), SPEC, fb)
            early = simulate_setup(cut, o.candidate, o.t, spec=SPEC, account=ACCOUNT, cost_model=COMMISSION)
            assert early.result == "end_of_data" and early.exit_index == e - 1
            assert (early.trade.entry, early.trade.volume, early.trade.take_profit) == (
                o.trade.entry, o.trade.volume, o.trade.take_profit)


def test_candidates_are_never_mutated(walk: HistoryData, walk_scan) -> None:
    before = copy.deepcopy([c.model_dump() for c in walk_scan.candidates])
    bars = build_bar_arrays(walk.h1, SPEC)
    outs = simulate_setups(bars, walk_scan.candidates, spec=SPEC, account=ACCOUNT, cost_model=COMMISSION)
    for o in outs:
        if o.trade is not None:
            o.trade.indicators["probe"] = 1.0  # a copy: must not reach the candidate
    assert [c.model_dump() for c in walk_scan.candidates] == before
    assert all("probe" not in c.extra for c in walk_scan.candidates)


# ---------------------------------------------------------------------------------------------- backtest window
def test_backtest_window_for_range_mapping_and_clipping(walk: HistoryData, walk_scan) -> None:
    times = pd.DatetimeIndex(walk.h1["time"]).as_unit("ns").asi8
    h4 = pd.DatetimeIndex(walk.h4["time"]).as_unit("ns").asi8
    earliest = earliest_start(times, h4, walk_scan.first_valid_index, 14)
    last = pd.Timestamp(int(times[-1]), tz="UTC") + pd.Timedelta(hours=1)
    # decisions [from, to] -> bars [from - 1h, to): hour-aligned range inside the data
    lo = (earliest + pd.Timedelta(days=7)).normalize()
    while lo.dayofweek != 1:  # a Tuesday, 10:00 UTC (the walk has no weekend bars)
        lo += pd.Timedelta(days=1)
    lo += pd.Timedelta(hours=10)
    w = backtest_window_for_range(lo, lo + pd.Timedelta(days=3), times, h4, walk_scan.first_valid_index, 14)
    assert w.available and not w.clipped and w.note_fa is None
    assert pd.Timestamp(w.start) == lo - pd.Timedelta(hours=1) and pd.Timestamp(w.end) == lo + pd.Timedelta(days=3)
    # not hour-aligned: from 10:30 -> first decision 11:00 -> bar 10:00; to 15:30 -> last decision 15:00 -> end 15:00
    w = backtest_window_for_range(lo + pd.Timedelta(minutes=30), lo + pd.Timedelta(hours=5, minutes=30), times, h4,
                                  walk_scan.first_valid_index, 14)
    assert pd.Timestamp(w.start) == lo and pd.Timestamp(w.end) == lo + pd.Timedelta(hours=5)
    # partly before the earliest start / after the data -> clipped with a Persian note
    w = backtest_window_for_range(earliest - pd.Timedelta(days=5), last + pd.Timedelta(days=5), times, h4,
                                  walk_scan.first_valid_index, 14)
    assert w.available and w.clipped and pd.Timestamp(w.start) == earliest and pd.Timestamp(w.end) == last
    assert "اولین زمان مجاز" in w.note_fa and "آخرین داده" in w.note_fa
    # entirely before the earliest start -> unavailable with the POST error
    w = backtest_window_for_range(earliest - pd.Timedelta(days=9), earliest - pd.Timedelta(days=2), times, h4,
                                  walk_scan.first_valid_index, 14)
    assert not w.available and w.code == "window_too_early" and "اولین زمان مجاز" in w.message_fa
    assert w.start is None and w.requested_start is not None
    w = backtest_window_for_range(last + pd.Timedelta(days=1), last + pd.Timedelta(days=2), times, h4,
                                  walk_scan.first_valid_index, 14)
    assert not w.available and w.code == "window_beyond_data"


def test_flags_match_a_manual_backtest_of_the_same_window(walk: HistoryData, walk_scan) -> None:
    times = pd.DatetimeIndex(walk.h1["time"]).as_unit("ns").asi8
    h4 = pd.DatetimeIndex(walk.h4["time"]).as_unit("ns").asi8
    earliest = earliest_start(times, h4, walk_scan.first_valid_index, 14)
    frm, to = earliest - pd.Timedelta(days=3), earliest + pd.Timedelta(days=40)
    w = backtest_window_for_range(frm, to, times, h4, walk_scan.first_valid_index, 14)
    assert w.available and w.clipped
    config = RunConfig(symbol=walk.symbol, mode="manual", start=w.start, end=w.end, account=ACCOUNT,
                       strategy_name=STRATEGY.name, strategy_version=STRATEGY.version, params=CLEAN, params_hash=HASH)
    res = run_backtest(config, walk, walk_scan).windows[0]
    decisions = [c for c in walk_scan.candidates if frm <= pd.Timestamp(c.decision_time_utc) <= to]
    flags = backtest_flags(res, decisions)
    traded = [c for c, f in zip(decisions, flags) if f.traded]
    assert [c.confirmation_bar_open_utc for c in traded] == [t.confirmation_bar_time for t in res.trades]
    assert [f.net_pnl for f in flags if f.traded] == [t.net_pnl for t in res.trades]
    reasons = {f.reason for f in flags if not f.traded}
    assert "outside_window" in reasons and "position_open" in reasons  # clipped part + overlaps
    for c, f in zip(decisions, flags):
        if f.reason == "outside_window":
            assert pd.Timestamp(c.confirmation_bar_open_utc) < pd.Timestamp(w.start)
            assert f.reason_fa == OUTSIDE_BACKTEST_WINDOW_FA
        assert f.traded or (f.reason_fa and any("؀" <= ch <= "ۿ" for ch in f.reason_fa))
    assert all(math.isfinite(f.net_pnl) for f in flags if f.traded)
