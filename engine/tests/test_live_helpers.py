"""Pure helpers of the live signals (``alpha_engine.signals.live``): indicative entry / levels / volume with worked
numbers, the entry step (gap class, expiry), the candidate comparison and the backtest-skip flag. No MT5, no DB.

Worked example (the numbers of the engine README's gold setup; default account: balance 2500, risk 1 % = 25.00,
leverage 100, rr 2; gold spec: contract 100, tick 0.01 / 1.00 -> 100 USD per 1.00 per lot, volume 0.01/0.01/100):

* BUY, SL 2062.8835288730947. Fresh tick bid 2064.37 / ask 2064.68 -> entry = ASK 2064.68 (the side the simulator
  fills a buy on); distance 1.7964711269053; TP (indicative) = 2064.68 + 2 * 1.7964711269 = 2068.2729422538;
  loss per lot 179.64711269 -> raw 25 / 179.64711269 = 0.13916 -> floor to 0.01 = 0.13 lots; actual risk
  0.13 * 179.64711269 = 23.354124650; margin 0.13 * 100 * 2064.68 / 100 = 268.4084.
* Same setup, market closed after the bar (stale tick): entry = last close 2064.10 + spread 34 points (0.34) =
  2064.44 (the ask); distance 1.5564711269; TP 2067.5529422538; raw 25 / 155.64711269 = 0.16062 -> 0.16 lots;
  risk 24.903538; margin 330.3104.
* SELL, SL 2066.00, tick bid 2064.37 -> entry = BID 2064.37; distance 1.63; TP 2064.37 - 3.26 = 2061.11;
  raw 25 / 163 = 0.15337 -> 0.15 lots; risk 24.45; margin 309.6555.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from alpha_engine.backtest.history import build_bar_arrays
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.signals.live import (
    ENTRY_UNKNOWN_FA,
    LEVELS_INVALID_FA,
    SIZING_REJECTED_FA,
    backtest_skip_flag,
    candidate_diff,
    choose_entry,
    entry_step_info,
    indicative_levels,
)
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategy.signal import SignalCandidate

GOLD = SymbolSpec(name="XAUUSD.x", digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                  currency_base="XAU")
BRENT = SymbolSpec(name="BRNUSD.x", digits=2, point=0.01, trade_contract_size=1000.0, trade_tick_value=10.0,
                   trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0, currency_profit="USD",
                   currency_base="BRN")
ACCOUNT = AccountSettings()  # 2500 / 1 % / 100 / rr 2
CONF = datetime(2025, 4, 28, 12, 0, tzinfo=timezone.utc)
H1 = timedelta(hours=1)


def cand(direction: str = "buy", *, symbol: str = "XAUUSD.x", ref: float = 2064.10,
         sl: float = 2062.8835288730947, conf: datetime = CONF) -> SignalCandidate:
    return SignalCandidate(
        strategy_name="scheduled_buy", strategy_version=1, params_hash="a" * 64, symbol=symbol, direction=direction,
        setup="scheduled", line=None, decision_time_utc=conf + H1, confirmation_bar_open_utc=conf,
        reference_price=ref, stop_loss=sl, rr=2.0, pattern="scheduled", reason_fa="آزمایشی", extra={"x": 1.0})


# ---------------------------------------------------------------------------------------------- indicative levels
def test_buy_with_a_fresh_tick_uses_the_ask() -> None:
    lv = indicative_levels(cand(), tick_bid=2064.37, tick_ask=2064.68, tick_fresh=True, last_close=2064.10,
                           last_spread_price=0.34, last_spread_points=34, spec=GOLD, account=ACCOUNT)
    assert lv.accepted and lv.entry == 2064.68 and lv.entry_source == "tick" and lv.spread_points is None
    assert lv.stop_loss == 2062.8835288730947
    assert lv.take_profit == pytest.approx(2068.2729422538, abs=1e-9)
    assert lv.take_profit == 2064.68 + 2 * (2064.68 - 2062.8835288730947)  # = cand.resolve_levels(entry)
    s = lv.sizing
    assert s is not None and s.volume == 0.13 and s.raw_volume == pytest.approx(0.139162, abs=1e-6)
    assert s.risk_amount == 25.0 and s.actual_risk == pytest.approx(23.354124650, abs=1e-8)
    assert s.margin == pytest.approx(268.4084, abs=1e-9)


def test_buy_without_a_fresh_tick_uses_last_close_plus_spread() -> None:
    lv = indicative_levels(cand(), tick_bid=2050.0, tick_ask=2050.3, tick_fresh=False, last_close=2064.10,
                           last_spread_price=0.34, last_spread_points=34, spec=GOLD, account=ACCOUNT)
    assert lv.entry == pytest.approx(2064.44, abs=1e-9) and lv.entry_source == "last_close" and lv.spread_points == 34
    assert lv.take_profit == pytest.approx(2067.5529422538, abs=1e-9)
    assert lv.sizing is not None and lv.sizing.volume == 0.16
    assert lv.sizing.actual_risk == pytest.approx(24.903538030, abs=1e-8)
    assert lv.sizing.margin == pytest.approx(330.3104, abs=1e-9)


def test_sell_uses_the_bid_tick_and_the_bare_close_as_fallback() -> None:
    sell = cand("sell", sl=2066.00)
    lv = indicative_levels(sell, tick_bid=2064.37, tick_ask=2064.68, tick_fresh=True, last_close=2064.10,
                           last_spread_price=0.34, last_spread_points=34, spec=GOLD, account=ACCOUNT)
    assert lv.entry == 2064.37 and lv.take_profit == pytest.approx(2061.11, abs=1e-9)
    assert lv.sizing is not None and lv.sizing.volume == 0.15
    assert lv.sizing.actual_risk == pytest.approx(24.45, abs=1e-9) and lv.sizing.margin == pytest.approx(309.6555)
    stale = indicative_levels(sell, tick_bid=None, tick_ask=None, tick_fresh=False, last_close=2064.10,
                              last_spread_price=0.34, last_spread_points=34, spec=GOLD, account=ACCOUNT)
    assert stale.entry == 2064.10 and stale.entry_source == "last_close" and stale.spread_points is None


def test_brent_example() -> None:
    lv = indicative_levels(cand(symbol="BRNUSD.x", ref=80.00, sl=79.50), tick_bid=80.02, tick_ask=80.05,
                           tick_fresh=True, last_close=80.0, last_spread_price=0.03, last_spread_points=3, spec=BRENT,
                           account=ACCOUNT)
    assert lv.entry == 80.05 and lv.take_profit == pytest.approx(81.15, abs=1e-9)
    assert lv.sizing is not None and lv.sizing.volume == 0.04  # 25 / (0.55 * 1000) = 0.04545
    assert lv.sizing.margin == pytest.approx(32.02, abs=1e-9)


def test_price_beyond_the_stop_is_rejected_like_a_gap_through_the_stop() -> None:
    lv = indicative_levels(cand(), tick_bid=2062.50, tick_ask=2062.80, tick_fresh=True, last_close=2064.10,
                           last_spread_price=0.34, last_spread_points=34, spec=GOLD, account=ACCOUNT)
    assert not lv.accepted and lv.rejection_reason_fa == LEVELS_INVALID_FA
    assert lv.take_profit is None and lv.sizing is None and lv.entry == 2062.80


def test_sizing_rejection_keeps_levels_and_gives_the_persian_reason() -> None:
    lv = indicative_levels(cand(), tick_bid=2064.37, tick_ask=2064.68, tick_fresh=True, last_close=2064.10,
                           last_spread_price=0.34, last_spread_points=34, spec=GOLD,
                           account=AccountSettings(balance=10.0))
    assert not lv.accepted and lv.rejection_reason_fa is not None
    assert lv.rejection_reason_fa.startswith(SIZING_REJECTED_FA) and "حداقل حجم" in lv.rejection_reason_fa
    assert lv.take_profit is not None and lv.sizing is not None and not lv.sizing.accepted


@pytest.mark.parametrize("bid,ask,fresh,expected", [
    (2064.37, 2064.68, True, (2064.68, "tick")),
    (2064.37, 0.0, True, (2064.44, "last_close")),  # broker sent no ask
    (2064.37, float("nan"), True, (2064.44, "last_close")),
    (2064.37, 2064.68, False, (2064.44, "last_close")),
])
def test_choose_entry(bid, ask, fresh, expected) -> None:
    entry, source = choose_entry("buy", tick_bid=bid, tick_ask=ask, tick_fresh=fresh, last_close=2064.10,
                                 last_spread_price=0.34)
    assert (round(entry, 9), source) == (expected[0], expected[1])


# ---------------------------------------------------------------------------------------------- entry step
def _times(*stamps: str) -> np.ndarray:
    return pd.DatetimeIndex([pd.Timestamp(s) for s in stamps]).as_unit("ns").asi8


def _hourly(start: str, hours: int, drop: tuple[str, ...] = ()) -> np.ndarray:
    idx = pd.date_range(start, periods=hours, freq="h", tz="UTC")
    return idx[~idx.isin(pd.DatetimeIndex([pd.Timestamp(d) for d in drop]))].as_unit("ns").asi8


def test_entry_step_one_hour_is_final() -> None:
    times = _hourly("2025-04-28T00:00Z", 6)
    step = entry_step_info(times, 2, tick_utc=None)
    assert (step.kind, step.provisional) == ("none", False)
    assert step.entry_bar_open == datetime(2025, 4, 28, 3, tzinfo=timezone.utc)
    assert step.expires == datetime(2025, 4, 28, 4, tzinfo=timezone.utc)


def test_entry_step_over_the_weekend_is_final() -> None:
    times = _times("2025-04-25T19:00Z", "2025-04-25T20:00Z", "2025-04-27T22:00Z", "2025-04-27T23:00Z")
    step = entry_step_info(times, 1, tick_utc=None)
    assert (step.kind, step.provisional, step.note_fa) == ("weekend", False, None)
    assert step.expires == datetime(2025, 4, 27, 23, tzinfo=timezone.utc)


def test_entry_step_one_off_hole_is_missing_but_provisional() -> None:
    times = _hourly("2025-04-28T00:00Z", 20, drop=("2025-04-28T10:00Z",))
    t = int(np.searchsorted(times, pd.Timestamp("2025-04-28T09:00Z").value))
    step = entry_step_info(times, t, tick_utc=None)
    assert (step.kind, step.provisional) == ("missing", True) and "۲۸ روز" in (step.note_fa or "")
    assert step.entry_bar_open == datetime(2025, 4, 28, 11, tzinfo=timezone.utc)


def test_entry_step_not_cached_uses_the_tick() -> None:
    times = _hourly("2025-04-28T00:00Z", 13)  # last bar 12:00 -> decision 13:00
    t = len(times) - 1
    forming = entry_step_info(times, t, tick_utc=datetime(2025, 4, 28, 13, 0, 30, tzinfo=timezone.utc))
    assert (forming.kind, forming.provisional) == ("none", False)
    assert forming.expires == datetime(2025, 4, 28, 14, tzinfo=timezone.utc)
    later = entry_step_info(times, t, tick_utc=datetime(2025, 4, 28, 15, 0, 5, tzinfo=timezone.utc))
    assert (later.kind, later.provisional) == ("unknown", True)
    assert later.entry_bar_open == datetime(2025, 4, 28, 15, tzinfo=timezone.utc)
    closed = entry_step_info(times, t, tick_utc=datetime(2025, 4, 28, 12, 59, 59, tzinfo=timezone.utc))
    assert (closed.kind, closed.provisional, closed.expires, closed.note_fa) == ("unknown", True, None, ENTRY_UNKNOWN_FA)
    assert entry_step_info(times, t, tick_utc=None).expires is None


# ---------------------------------------------------------------------------------------------- comparison
def test_candidate_diff_is_exact() -> None:
    a = cand()
    assert candidate_diff(a, cand()) == [] and candidate_diff(None, None) == []
    assert candidate_diff(a, None) == ["<presence>"] and candidate_diff(None, a.model_dump(mode="json")) == ["<presence>"]
    b = a.model_copy(update={"extra": {"x": 1.0000000000000002}})
    assert candidate_diff(a, b) == ["extra.x"]  # no tolerance
    c = a.model_copy(update={"reason_fa": "دیگر", "stop_loss": 2062.0})
    assert candidate_diff(a.model_dump(mode="json"), c) == ["reason_fa", "stop_loss"]


# ---------------------------------------------------------------------------------------------- backtest-skip flag
def _flat_bars(n: int = 30):
    times = pd.date_range("2025-04-28T00:00Z", periods=n, freq="h", tz="UTC")
    h1 = pd.DataFrame({"time": times, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                       "tick_volume": 1, "spread": 10, "real_volume": 0})
    return h1, build_bar_arrays(h1, GOLD)


def test_skip_flag_follows_the_backtest_one_trade_rule() -> None:
    h1, bars = _flat_bars()
    times = list(h1["time"])
    c10 = cand(ref=100.0, sl=99.0, conf=times[10].to_pydatetime())
    c12 = cand(ref=100.0, sl=99.0, conf=times[12].to_pydatetime())
    by_bar = {10: c10, 12: c12}
    # chain from bar 10: the trade opened at bar 11 is still open (flat prices) -> bar 12 is skipped
    flag = backtest_skip_flag(bars, by_bar, start_index=10, t=12, spec=GOLD, account=ACCOUNT)
    assert flag.would_skip and flag.reason == "position_open" and "معامله باز" in (flag.reason_fa or "")
    assert flag.chain_candidates == 2 and flag.chain_start == times[10].to_pydatetime()
    assert "open buy since 2025-04-28T11:00:00+00:00" in (flag.detail or "")
    # a chain starting at bar 12 (the first live signal) is flat
    alone = backtest_skip_flag(bars, by_bar, start_index=12, t=12, spec=GOLD, account=ACCOUNT)
    assert not alone.would_skip and alone.reason is None and alone.chain_candidates == 1
    # the first candidate of a chain is never skipped
    first = backtest_skip_flag(bars, by_bar, start_index=0, t=10, spec=GOLD, account=ACCOUNT)
    assert not first.would_skip
