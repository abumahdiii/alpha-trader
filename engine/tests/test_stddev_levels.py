"""Entry / SL / TP rules of the StdDev-channel system (stddev-channel-system.md section 5).

Worked example (levels.py docstring): buy, rr 2, sl_atr_mult 0.2, confirmation low 1998.40, ATR_H1 2.50
    SL = 1998.40 - 0.50 = 1997.90; close 2000.00 -> indicative TP 2004.20
    next bar opens 2001.00 -> TP = 2001.00 + 2 * 3.10 = 2007.20
"""

from __future__ import annotations

import pytest

from alpha_engine.indicators.atr import wilder_atr
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import StdDevChannelStrategy
from alpha_engine.strategies.stddev_channel.levels import (
    TradeLevels,
    entry_price_for,
    resolve_trade_levels,
    stop_loss_price,
    take_profit_price,
)
from alpha_engine.strategy.base import evaluate_checked
from alpha_engine.strategy.signal import SignalCandidate
from fixtures.channel_scenarios import DEFAULT_T, build

STRATEGY = StdDevChannelStrategy()
T = DEFAULT_T


def test_worked_example_numbers() -> None:
    sl = stop_loss_price("buy", bar_high=2003.0, bar_low=1998.40, atr_h1=2.50, sl_atr_mult=0.2)
    assert sl == pytest.approx(1997.90, abs=1e-9)
    assert take_profit_price(2000.0, sl, 2.0, "buy") == pytest.approx(2004.20, abs=1e-9)
    assert take_profit_price(2001.0, sl, 2.0, "buy") == pytest.approx(2007.20, abs=1e-9)
    # Sell mirror: high 2001.60 + 0.50 = 2002.10; entry 1999.00 -> TP 1999 - 2 * 3.10 = 1992.80
    sl_sell = stop_loss_price("sell", bar_high=2001.60, bar_low=1996.0, atr_h1=2.50, sl_atr_mult=0.2)
    assert sl_sell == pytest.approx(2002.10, abs=1e-9)
    assert take_profit_price(1999.0, sl_sell, 2.0, "sell") == pytest.approx(1992.80, abs=1e-9)


def test_negative_or_nan_buffer_rejected() -> None:
    with pytest.raises(ValueError):
        stop_loss_price("buy", 2003.0, 1998.4, float("nan"), 0.2)
    with pytest.raises(ValueError):
        stop_loss_price("buy", 2003.0, 1998.4, 2.5, -0.1)


@pytest.mark.parametrize(("trend", "side", "kind"), [
    ("up", "buy", "bounce_outer"), ("up", "buy", "breakout_outer"), ("down", "sell", "bounce_outer"),
    ("down", "sell", "breakout_mid"), ("flat", "sell", "bounce_mid"),
])
def test_candidate_stop_loss_from_confirmation_bar_and_h1_atr(trend: str, side: str, kind: str) -> None:
    sc = build(trend, side, kind)
    cand = evaluate_checked(STRATEGY, sc.ctx())
    h1 = sc.h1.iloc[: T + 1]
    atr = float(wilder_atr(h1["high"], h1["low"], h1["close"], 14).iloc[-1])  # independent recomputation
    bar = sc.h1.iloc[T]
    expected = bar["low"] - atr * 0.2 if side == "buy" else bar["high"] + atr * 0.2
    assert cand.stop_loss == expected
    assert cand.extra["atr_h1"] == atr and cand.extra["sl_buffer"] == atr * 0.2
    assert cand.reference_price == bar["close"]
    assert cand.indicative_take_profit == pytest.approx(
        bar["close"] + (2.0 if side == "buy" else -2.0) * abs(bar["close"] - expected), rel=1e-15)


def test_sl_atr_mult_param_and_zero_buffer() -> None:
    sc = build("up", "buy", "bounce_outer", params={"sl_atr_mult": 0.0})
    cand = evaluate_checked(STRATEGY, sc.ctx())
    assert cand.stop_loss == sc.h1["low"].iloc[T]
    sc = build("up", "buy", "bounce_outer", params={"sl_atr_mult": 1.0})
    cand1 = evaluate_checked(STRATEGY, sc.ctx())
    assert cand1.stop_loss == pytest.approx(sc.h1["low"].iloc[T] - cand1.extra["atr_h1"], rel=1e-15)


def test_entry_is_next_bar_open_and_unknown_at_decision() -> None:
    sc = build("up", "buy", "bounce_outer")
    cand = evaluate_checked(STRATEGY, sc.ctx(account=AccountSettings(rr=3.0)))
    assert "entry" not in SignalCandidate.model_fields and "entry_price" not in SignalCandidate.model_fields
    assert cand.entry_rule == "next_bar_open"
    # The fill bar t+1 opens exactly at the decision time.
    assert cand.fill_bar_open_utc == sc.h1["time"].iloc[T + 1]
    h1_prefix = sc.h1.iloc[: T + 1]
    assert entry_price_for(h1_prefix, T) is None  # t+1 does not exist yet at decision time
    entry = entry_price_for(sc.h1, T)
    assert entry == sc.h1["open"].iloc[T + 1]
    levels = resolve_trade_levels(cand, entry)
    assert isinstance(levels, TradeLevels)
    assert levels.entry == entry and levels.stop_loss == cand.stop_loss
    assert levels.take_profit == entry + 3.0 * (entry - cand.stop_loss)
    assert levels.risk_distance == entry - cand.stop_loss


def test_changing_the_next_bar_open_changes_only_the_resolved_tp() -> None:
    sc = build("down", "sell", "bounce_outer")
    before = evaluate_checked(STRATEGY, sc.ctx())
    sc.h1.loc[T + 1, ["open", "high", "low", "close"]] = [1.0e4, 1.0e4 + 1, 1.0e4 - 1, 1.0e4]
    after = evaluate_checked(STRATEGY, sc.ctx())
    assert after == before  # the decision never sees bar t+1
    entry = before.stop_loss - 5.0
    assert resolve_trade_levels(before, entry).take_profit == entry - 2.0 * 5.0


def test_gap_through_stop_is_rejected_at_fill_time() -> None:
    sc = build("up", "buy", "bounce_outer")
    cand = evaluate_checked(STRATEGY, sc.ctx())
    with pytest.raises(ValueError, match="gap through the stop"):
        resolve_trade_levels(cand, cand.stop_loss - 0.01)
    with pytest.raises(ValueError):
        resolve_trade_levels(cand, cand.stop_loss)
