"""Hand-built micro scenarios for the bar-by-bar simulator (backtest/simulator.py).

Every expected number is computed by hand in the comments. Symbol: gold-like spec (point 0.01, tick_size
0.01, tick_value 1.00 -> 100 USD per 1.00 price move per lot, volume 0.01/0.01/100, contract 100).
Bars are BID prices; ``spread`` is in points (x 0.01).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from alpha_engine.backtest.costs import causal_spread
from alpha_engine.backtest.history import build_bar_arrays
from alpha_engine.backtest.models import CostModel, ExitReason, SkipReason, Window
from alpha_engine.backtest.simulator import held_over_weekend, scan_provider, simulate_window
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategy.signal import Setup, SignalCandidate

SPEC = SymbolSpec(name="XAUUSD.x", digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                  currency_base="XAU")
T0 = pd.Timestamp("2026-01-06 00:00", tz="UTC")  # Tuesday
HASH = "a" * 64


def frame(rows: list[tuple], start: pd.Timestamp = T0, times: list[pd.Timestamp] | None = None) -> pd.DataFrame:
    """rows = (open, high, low, close, spread_points); consecutive hours unless ``times`` is given."""
    times = times or [start + pd.Timedelta(hours=i) for i in range(len(rows))]
    return pd.DataFrame({
        "time": pd.DatetimeIndex(times), "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows], "tick_volume": 1,
        "spread": [r[4] for r in rows], "real_volume": 0,
    })


def cand(h1: pd.DataFrame, i: int, direction: str, sl: float, rr: float = 2.0) -> SignalCandidate:
    conf = h1["time"].iloc[i].to_pydatetime()
    return SignalCandidate(
        strategy_name="stddev_channel", strategy_version=1, params_hash=HASH, symbol=SPEC.name, direction=direction,
        setup=Setup.BOUNCE_LOWER if direction == "buy" else Setup.BOUNCE_UPPER,
        line="lower" if direction == "buy" else "upper", decision_time_utc=conf + timedelta(hours=1),
        confirmation_bar_open_utc=conf, reference_price=float(h1["close"].iloc[i]), stop_loss=sl, rr=rr,
        pattern="pin_bar", reason_fa="آزمایشی",
    )


def run(h1: pd.DataFrame, cands: dict[int, SignalCandidate], *, balance: float = 10_000.0, risk: float = 1.0,
        leverage: int = 100, commission: float = 0.0, window: tuple[int, int] | None = None):
    bars = build_bar_arrays(h1, SPEC)
    lo, hi = window or (0, len(h1))
    start = h1["time"].iloc[lo].to_pydatetime()
    end = (h1["time"].iloc[hi - 1] + pd.Timedelta(hours=1)).to_pydatetime() if hi == len(h1) else \
        h1["time"].iloc[hi].to_pydatetime()
    rr = next(iter(cands.values())).rr if cands else 2.0
    return simulate_window(
        bars, Window(index=0, start=start, end=end), scan_provider(cands), spec=SPEC,
        account=AccountSettings(balance=balance, risk_pct=risk, leverage=leverage, rr=rr),
        cost_model=CostModel(commission_per_lot_per_side=commission),
    )


# ---------------------------------------------------------------------------------------------- long
def test_long_take_profit_worked_example() -> None:
    """Worked example (long): balance 10000, risk 1 % = 100, rr 2, commission 3.50/lot/side.

    bar 1 opens (bid) 2000.00, spread 25 pts -> fill = ask = 2000.25; SL 1995.00 -> distance 5.25
    TP = 2000.25 + 2 * 5.25 = 2010.75; raw volume = 100 / (5.25 * 100) = 0.190476 -> floor 0.19
    risk_amount = 0.19 * 5.25 * 100 = 99.75
    bar 1 close 2002.00: equity = 10000 + (2002.00 - 2000.25) * 0.19 * 100 - 1.33 = 10031.92
    bar 2 high 2011.00 >= TP -> exit at 2010.75: gross = 10.50 * 0.19 * 100 = 199.50
    commission = 3.50 * 0.19 * 2 = 1.33 -> net 198.17; R = 198.17 / 99.75 = 1.986667; balance 10198.17
    """
    h1 = frame([
        (2001.00, 2002.00, 1999.00, 2000.50, 25),  # 0: confirmation bar
        (2000.00, 2003.00, 1999.50, 2002.00, 25),  # 1: entry bar
        (2002.00, 2011.00, 2001.50, 2010.90, 25),  # 2: TP hit
        (2010.00, 2011.00, 2009.00, 2010.50, 25),
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)}, commission=3.5)
    assert len(res.trades) == 1 and not res.skipped
    tr = res.trades[0]
    assert tr.entry == pytest.approx(2000.25) and tr.entry_bid_open == 2000.00
    assert tr.stop_loss == 1995.00 and tr.take_profit == pytest.approx(2010.75)
    assert tr.volume == 0.19 and tr.risk_amount == pytest.approx(99.75)
    assert tr.exit_reason is ExitReason.TP and tr.exit_price == pytest.approx(2010.75)
    assert tr.gross_pnl == pytest.approx(199.50) and tr.commission == pytest.approx(1.33)
    assert tr.net_pnl == pytest.approx(198.17) and tr.r_multiple == pytest.approx(198.17 / 99.75)
    assert tr.balance_after == pytest.approx(10198.17) and res.final_balance == pytest.approx(10198.17)
    assert tr.entry_time == h1["time"].iloc[1].to_pydatetime()
    assert tr.exit_bar_time == h1["time"].iloc[2].to_pydatetime()
    assert tr.exit_time == (h1["time"].iloc[2] + pd.Timedelta(hours=1)).to_pydatetime()  # intrabar -> bar close
    assert tr.spread_at_entry_points == 25 and tr.bars_held == 2 and not tr.held_over_weekend
    # equity: initial point + one per bar close
    eq = [p.equity for p in res.equity]
    assert eq[0] == 10_000 and eq[1] == 10_000
    assert eq[2] == pytest.approx(10031.92)
    assert eq[3] == pytest.approx(10198.17) and eq[4] == pytest.approx(10198.17)
    assert [p.time for p in res.equity][1] == (h1["time"].iloc[0] + pd.Timedelta(hours=1)).to_pydatetime()


def test_long_stop_loss_hit_exactly_at_sl() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1998.00, 1999.00, 25),  # entry 2000.25, SL 1995, TP 2010.75
        (1999.00, 1999.50, 1994.00, 1996.00, 25),  # low 1994 <= 1995 -> SL at 1995
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    # gross = (1995 - 2000.25) * 0.19 * 100 = -99.75 = -risk_amount -> R = -1
    assert tr.exit_reason is ExitReason.SL and tr.exit_price == 1995.00
    assert tr.gross_pnl == pytest.approx(-99.75) and tr.r_multiple == pytest.approx(-1.0)


def test_same_bar_sl_and_tp_counts_as_sl() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),
        (2000.00, 2012.00, 1994.00, 2005.00, 25),  # touches TP 2010.75 and SL 1995
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    assert tr.exit_reason is ExitReason.SL and tr.exit_price == 1995.00


def test_sl_and_tp_can_hit_on_the_entry_bar() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2011.00, 1999.00, 2010.00, 25),  # entry bar itself reaches TP 2010.75
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    assert tr.exit_reason is ExitReason.TP and tr.bars_held == 1


def test_long_gap_through_stop_at_entry_is_skipped() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (1994.00, 1996.00, 1993.00, 1995.50, 25),  # bid open 1994 <= SL 1995
        (1995, 1996, 1994, 1995, 25),
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)})
    assert not res.trades and [s.reason for s in res.skipped] == [SkipReason.GAP_THROUGH_STOP]


def test_long_bid_open_at_stop_is_skipped_even_if_ask_is_above() -> None:
    """bid open 1995.00 == SL, ask 1995.25 > SL: the stop (bid side) would trigger at once -> skipped."""
    h1 = frame([(2001, 2002, 1999, 2000.5, 25), (1995.00, 1997.00, 1994.50, 1996.00, 25)])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)})
    assert not res.trades and res.skipped[0].reason is SkipReason.GAP_THROUGH_STOP


def test_long_gap_at_open_through_sl_and_beyond_tp() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),
        (1990.00, 1991.00, 1989.00, 1990.50, 25),  # opens 1990 < SL 1995 -> sl_gap @ 1990
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    assert tr.exit_reason is ExitReason.SL_GAP and tr.exit_price == 1990.00
    assert tr.exit_time == h1["time"].iloc[2].to_pydatetime()  # gap exit -> bar open
    # gross = (1990 - 2000.25) * 0.19 * 100 = -194.75 (worse than -1 R)
    assert tr.gross_pnl == pytest.approx(-194.75)

    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),
        (2015.00, 2016.00, 2014.00, 2015.50, 25),  # opens 2015 > TP 2010.75 -> tp_gap @ 2015
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    assert tr.exit_reason is ExitReason.TP_GAP and tr.exit_price == 2015.00
    assert tr.gross_pnl == pytest.approx((2015.00 - 2000.25) * 0.19 * 100)


# ---------------------------------------------------------------------------------------------- short
def test_short_stopped_on_the_ask_side_worked_example() -> None:
    """Worked example (short): balance 10000, risk 1 %, rr 2, commission 3.50/lot/side.

    bar 1 bid open 2000.00, spread 30 -> ask 2000.30 < SL 2005.00: fill = bid open 2000.00
    distance 5.00; TP = 2000.00 - 2 * 5.00 = 1990.00; raw volume 100 / 500 = 0.2 -> 0.20; risk 100.00
    bar 2: bid high 2004.80 < SL, but ask high = 2004.80 + 0.30 = 2005.10 >= SL -> stopped at 2005.00
    gross = (2005.00 - 2000.00) * -1 * 0.20 * 100 = -100.00; commission = 3.50 * 0.20 * 2 = 1.40
    net = -101.40; R = -1.014; balance 9898.60
    """
    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 30),  # 0: confirmation
        (2000.00, 2001.00, 1999.00, 2000.20, 30),  # 1: entry bar
        (2000.20, 2004.80, 1999.50, 2003.00, 30),  # 2: ask high 2005.10
    ])
    res = run(h1, {0: cand(h1, 0, "sell", 2005.00)}, commission=3.5)
    tr = res.trades[0]
    assert tr.entry == 2000.00 and tr.take_profit == pytest.approx(1990.00)
    assert tr.volume == 0.20 and tr.risk_amount == pytest.approx(100.0)
    assert tr.exit_reason is ExitReason.SL and tr.exit_price == 2005.00
    assert tr.gross_pnl == pytest.approx(-100.0) and tr.commission == pytest.approx(1.40)
    assert tr.net_pnl == pytest.approx(-101.40) and tr.r_multiple == pytest.approx(-1.014)
    assert tr.balance_after == pytest.approx(9898.60)
    assert tr.spread_at_exit_points == 30
    # bar 1 close MTM on the ask: (2000.20 + 0.30 - 2000.00) * -1 * 0.2 * 100 - 1.40 = -11.40
    assert res.equity[2].equity == pytest.approx(10_000 - 11.40)


def test_short_bid_high_below_sl_without_spread_is_not_stopped() -> None:
    """Control for the ask-side test: the same bar with spread 0 everywhere (unfilled) -> no stop."""
    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 0),
        (2000.00, 2001.00, 1999.00, 2000.20, 0),
        (2000.20, 2004.80, 1999.50, 2003.00, 0),
    ])
    res = run(h1, {0: cand(h1, 0, "sell", 2005.00)})
    tr = res.trades[0]
    assert tr.exit_reason is ExitReason.END_OF_PERIOD and tr.exit_price == 2003.00
    assert res.zero_spread_bars_unfilled == 3 and "entry_spread_zero" in tr.flags


def test_short_take_profit_on_ask_side() -> None:
    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 30),
        (2000.00, 2001.00, 1999.00, 2000.20, 30),
        (1995.00, 1996.00, 1989.80, 1992.00, 30),  # ask low 1990.10 > TP 1990 -> no TP
        (1992.00, 1993.00, 1989.60, 1991.00, 30),  # ask low 1989.90 <= TP -> TP @ 1990
    ])
    tr = run(h1, {0: cand(h1, 0, "sell", 2005.00)}).trades[0]
    assert tr.exit_reason is ExitReason.TP and tr.exit_price == pytest.approx(1990.00)
    assert tr.exit_bar_time == h1["time"].iloc[3].to_pydatetime()
    assert tr.gross_pnl == pytest.approx(10.0 * 0.20 * 100)


def test_short_gap_through_stop_on_ask_at_entry_is_skipped() -> None:
    """bid open 2004.90 < SL 2005 but ask open 2004.90 + 0.30 = 2005.20 >= SL -> skipped."""
    h1 = frame([(1999.00, 2001.00, 1998.50, 2000.50, 30), (2004.90, 2006.0, 2004.0, 2005.5, 30)])
    res = run(h1, {0: cand(h1, 0, "sell", 2005.00)})
    assert not res.trades and res.skipped[0].reason is SkipReason.GAP_THROUGH_STOP
    assert "ask open" in res.skipped[0].detail


def test_short_gap_exits_fill_at_ask_open() -> None:
    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 30),
        (2000.00, 2001.00, 1999.00, 2000.20, 30),
        (2006.00, 2007.00, 2005.50, 2006.50, 30),  # ask open 2006.30 >= SL -> sl_gap @ 2006.30
    ])
    tr = run(h1, {0: cand(h1, 0, "sell", 2005.00)}).trades[0]
    assert tr.exit_reason is ExitReason.SL_GAP and tr.exit_price == pytest.approx(2006.30)
    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 30),
        (2000.00, 2001.00, 1999.00, 2000.20, 30),
        (1985.00, 1986.00, 1984.00, 1985.50, 30),  # ask open 1985.30 <= TP 1990 -> tp_gap @ 1985.30
    ])
    tr = run(h1, {0: cand(h1, 0, "sell", 2005.00)}).trades[0]
    assert tr.exit_reason is ExitReason.TP_GAP and tr.exit_price == pytest.approx(1985.30)


# ---------------------------------------------------------------------------------------------- end of period
def test_end_of_period_long_at_bid_close_and_short_at_ask_close() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),
        (2000.00, 2003.00, 1999.00, 2002.00, 40),  # last bar, close 2002.00, spread 40
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)})
    tr = res.trades[0]
    assert tr.exit_reason is ExitReason.END_OF_PERIOD and tr.exit_price == 2002.00
    assert tr.exit_reason_fa == "بسته‌شده در پایان بازه"
    assert tr.exit_time == (h1["time"].iloc[2] + pd.Timedelta(hours=1)).to_pydatetime()
    assert res.equity[-1].equity == res.equity[-1].balance == pytest.approx(tr.balance_after)

    h1 = frame([
        (1999.00, 2001.00, 1998.50, 2000.50, 30),
        (2000.00, 2001.00, 1999.00, 2000.20, 30),
        (2000.00, 2001.00, 1998.00, 1999.00, 40),  # ask close = 1999.00 + 0.40 = 1999.40
    ])
    tr = run(h1, {0: cand(h1, 0, "sell", 2005.00)}).trades[0]
    assert tr.exit_reason is ExitReason.END_OF_PERIOD and tr.exit_price == pytest.approx(1999.40)
    assert tr.spread_at_exit_points == 40


# ---------------------------------------------------------------------------------------------- spread / costs
def test_causal_spread_fill() -> None:
    s = causal_spread(np.array([25, 0, 0, 31, 0]), 5, 0.01)
    assert s.points.tolist() == [25, 25, 25, 31, 31]
    assert s.filled.tolist() == [False, True, True, False, True] and not s.unfilled.any()
    assert s.price[1] == pytest.approx(0.25)
    s = causal_spread(np.array([0, 0, 12]), 3, 0.01)
    assert s.points.tolist() == [0, 0, 12] and s.unfilled.tolist() == [True, True, False]
    assert not causal_spread(None, 3, 0.01).has_data


def test_zero_spread_on_entry_bar_uses_previous_non_zero() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 0),  # spread 0 -> 25 (previous) -> fill 2000.25
        (2000.00, 2001.00, 1999.00, 2000.00, 0),
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)})
    tr = res.trades[0]
    assert tr.entry == pytest.approx(2000.25) and tr.spread_at_entry_points == 25
    assert "entry_spread_filled" in tr.flags and res.zero_spread_bars_filled == 2


def test_zero_spread_is_never_filled_from_the_future() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 0),
        (2000.00, 2001.00, 1999.00, 2000.00, 0),  # no earlier non-zero spread -> 0
        (2000.00, 2001.00, 1999.00, 2000.00, 50),
    ])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}).trades[0]
    assert tr.entry == 2000.00 and tr.spread_at_entry_points == 0 and "entry_spread_zero" in tr.flags


def test_commission_both_sides() -> None:
    h1 = frame([(2001, 2002, 1999, 2000.5, 25), (2000.00, 2001.00, 1999.00, 2000.00, 25),
                (2000.00, 2001.00, 1994.00, 1996.00, 25)])
    tr = run(h1, {0: cand(h1, 0, "buy", 1995.00)}, commission=7.0).trades[0]
    assert tr.commission == pytest.approx(7.0 * 0.19 * 2)
    assert tr.net_pnl == pytest.approx(tr.gross_pnl - 2.66)


# ---------------------------------------------------------------------------------------------- sizing / state
def test_second_trade_is_sized_off_the_new_balance() -> None:
    """Risk 5 %: trade 2 is sized from the realized balance after trade 1, not from the initial 10000."""
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),   # entry 2000.25, SL 1995 (5.25), TP 2010.75
        (2000.00, 2011.00, 1999.50, 2010.00, 25),   # TP
        (2010.00, 2011.00, 2009.00, 2010.50, 25),   # 3: confirmation of trade 2
        (2010.00, 2011.00, 2009.00, 2010.00, 25),   # 4: entry 2010.25, SL 2005 (5.25)
        (2010.00, 2011.00, 2009.00, 2010.00, 25),
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00), 3: cand(h1, 3, "buy", 2005.00)}, risk=5.0)
    t1, t2 = res.trades
    # trade 1: risk 500 -> 500 / 525 = 0.952 -> 0.95; gross = 10.50 * 0.95 * 100 = 997.50
    assert t1.volume == 0.95 and t1.balance_after == pytest.approx(10997.50)
    # trade 2: risk 5 % of 10997.50 = 549.875 -> 549.875 / 525 = 1.04738 -> 1.04 (initial balance would give 0.95)
    assert t2.balance_before == pytest.approx(10997.50) and t2.volume == 1.04


def test_candidate_while_position_open_is_dropped() -> None:
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.00, 2000.00, 25),  # entry; candidate at bar 1 -> dropped (trade open)
        (2000.00, 2001.00, 1999.00, 2000.00, 25),
        (2000.00, 2001.00, 1994.00, 1996.00, 25),  # SL -> closed; a candidate here IS taken (after exits)
        (1996.00, 1997.00, 1995.50, 1996.50, 25),  # entry of the bar-3 candidate
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00), 1: cand(h1, 1, "buy", 1990.00),
                   3: cand(h1, 3, "buy", 1993.00)})
    assert [s.reason for s in res.skipped] == [SkipReason.POSITION_OPEN]
    assert res.skipped[0].confirmation_bar_time == h1["time"].iloc[1].to_pydatetime()
    assert len(res.trades) == 2 and res.trades[1].entry_time == h1["time"].iloc[4].to_pydatetime()
    assert res.candidates == 3


def test_entry_bar_outside_window_is_not_entered() -> None:
    h1 = frame([(2001, 2002, 1999, 2000.5, 25)] * 2 + [(2000.0, 2001.0, 1999.0, 2000.0, 25)] * 2)
    res = run(h1, {1: cand(h1, 1, "buy", 1995.00)}, window=(0, 2))  # window = bars 0..1
    assert not res.trades and res.skipped[0].reason is SkipReason.ENTRY_OUTSIDE_WINDOW
    res = run(h1, {3: cand(h1, 3, "buy", 1995.00)})  # last bar of the data
    assert res.skipped[0].reason is SkipReason.ENTRY_OUTSIDE_WINDOW


def test_candidate_before_a_missing_gap_is_skipped() -> None:
    """A one-off 1-bar hole on a Tuesday is a ``missing`` gap (data/gaps.py)."""
    times = [T0 + pd.Timedelta(hours=h) for h in (0, 1, 2, 4, 5)]  # 03:00 missing
    h1 = frame([(2001, 2002, 1999, 2000.5, 25)] * 3 + [(2000.0, 2001.0, 1999.0, 2000.0, 25)] * 2, times=times)
    res = run(h1, {2: cand(h1, 2, "buy", 1995.00)})
    assert not res.trades and res.skipped[0].reason is SkipReason.MISSING_GAP


def weekend_frame() -> pd.DataFrame:
    fri = pd.Timestamp("2026-01-09 19:00", tz="UTC")
    sun = pd.Timestamp("2026-01-11 23:00", tz="UTC")
    times = [fri + pd.Timedelta(hours=h) for h in range(3)] + [sun + pd.Timedelta(hours=h) for h in range(3)]
    return frame([(2001, 2002, 1999, 2000.5, 25), (2000.0, 2001.0, 1999.0, 2000.0, 25),
                  (2000.0, 2001.0, 1999.0, 2000.0, 25), (2001.0, 2002.0, 2000.0, 2001.0, 25),
                  (2001.0, 2011.0, 2000.5, 2010.0, 25), (2010.0, 2011.0, 2009.0, 2010.0, 25)], times=times)


def test_trade_held_over_the_weekend_is_flagged() -> None:
    h1 = weekend_frame()
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)})
    tr = res.trades[0]
    assert tr.exit_reason is ExitReason.TP and tr.held_over_weekend and "held_over_weekend" in tr.flags
    assert res.weekend_holds == 1


def test_entry_after_the_weekend_is_sundays_open() -> None:
    h1 = weekend_frame()
    res = run(h1, {2: cand(h1, 2, "buy", 1995.00)})  # confirmation = Friday's last bar (21:00)
    tr = res.trades[0]
    assert tr.entry_time == datetime(2026, 1, 11, 23, tzinfo=timezone.utc) and tr.entry == pytest.approx(2001.25)
    assert not tr.held_over_weekend


def test_held_over_weekend_helper() -> None:
    ns = lambda s: pd.Timestamp(s, tz="UTC").value  # noqa: E731
    assert held_over_weekend(ns("2026-01-09 20:00"), ns("2026-01-12 01:00"))
    assert not held_over_weekend(ns("2026-01-06 20:00"), ns("2026-01-08 01:00"))


def test_sizing_rejection_is_recorded() -> None:
    h1 = frame([(2001, 2002, 1999, 2000.5, 25), (2000.00, 2001.00, 1999.00, 2000.00, 25)])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00)}, balance=100.0)  # 1.00 / 525 -> 0.0019 < 0.01
    assert not res.trades and res.skipped[0].reason is SkipReason.SIZING_REJECTED
    assert "حداقل حجم" in res.skipped[0].reason_fa


def test_window_stops_when_balance_is_depleted() -> None:
    """balance 100, risk 10 % = 10 -> 10 / (0.5 * 100) = 0.2 lot (margin 0.2*100*2000/1000 = 40 at 1:1000);
    the next bar opens 1000 below the SL -> loss (1000 - 2000.25) * 0.2 * 100 = -20005 -> balance < 0."""
    h1 = frame([
        (2001, 2002, 1999, 2000.5, 25),
        (2000.00, 2001.00, 1999.90, 2000.00, 25),  # fill 2000.25, SL 1999.75
        (1000.00, 1001.00, 999.00, 1000.00, 25),
        (1000.00, 1001.00, 999.00, 1000.00, 25),
    ])
    res = run(h1, {0: cand(h1, 0, "buy", 1999.75)}, balance=100.0, risk=10.0, leverage=1000)
    assert res.stopped_reason == "balance_depleted" and res.final_balance < 0
    assert res.bars == 3 and len(res.equity) == 4
    assert res.trades[0].exit_reason is ExitReason.SL_GAP


def test_results_are_json_serialisable() -> None:
    h1 = frame([(2001, 2002, 1999, 2000.5, 25), (2000.0, 2001.0, 1999.0, 2000.0, 25),
                (2000.0, 2011.0, 1999.5, 2010.0, 25)])
    res = run(h1, {0: cand(h1, 0, "buy", 1995.00), 1: cand(h1, 1, "buy", 1990.0)})
    data = res.model_dump(mode="json")
    assert data["trades"][0]["exit_reason"] == "tp" and data["skipped"][0]["reason"] == "position_open"
    assert type(res).model_validate(data) == res
    assert math.isfinite(data["equity"][-1]["equity"])
