"""Account settings consumption (phase 2 section 5): build_context and size_from_spec.

* ``build_context`` returns exactly what ``PUT /settings`` and ``PUT /strategies`` stored, and only
  H4 bars closed at the decision time;
* ``size_from_spec`` with the REAL broker specs (cached from MT5 on 2026-09-27, copied here as
  constants -- tests never read ``data/``) reproduces the worked examples:
  gold SL 5.00 -> 0.02 lots, margin 40.00; brent SL 0.50 -> 0.02 lots, margin 16.00.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alpha_engine import logging_setup
from alpha_engine.app import create_app
from alpha_engine.data.symbols import SymbolSpec
from alpha_engine.risk import size_from_spec
from alpha_engine.storage.account_settings import AccountSettings
from alpha_engine.strategies.stddev_channel import SCHEMA, StdDevChannelStrategy
from alpha_engine.strategy.base import H4_DURATION, LookAheadError, assert_closed_bars, evaluate_checked
from alpha_engine.strategy.context import NoClosedBarError, build_context, load_active_params
from alpha_engine.strategy.params import params_hash
from alpha_engine.strategy.signal import SignalCandidate
from fixtures.channel_scenarios import random_walk

# Real symbol_info values of the broker (data/cache/symbols/*.json, fetched 2026-09-27).
GOLD = SymbolSpec(name="XAUUSD.x", digits=2, point=0.01, trade_contract_size=100.0, trade_tick_value=1.0,
                  trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                  currency_profit="USD", currency_base="USD", description="Gold vs US Dollar")
BRENT = SymbolSpec(name="BRNUSD.x", digits=2, point=0.01, trade_contract_size=1000.0, trade_tick_value=10.0,
                   trade_tick_size=0.01, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                   currency_profit="USD", currency_base="USD", description="Oil - Brent Crude")
DEFAULT_ACCOUNT = AccountSettings()  # balance 2500 (since 2026-09-27), risk 1 %, leverage 100, rr 2
# Explicit account for the hand-verified sizing worked examples (independent of the defaults).
WORKED_ACCOUNT = AccountSettings(balance=1000.0, risk_pct=1.0, leverage=100, rr=2.0)


def _utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def _bars(times: list[str], price: float = 2000.0) -> pd.DataFrame:
    return pd.DataFrame({"time": [_utc(t) for t in times], "open": price, "high": price + 1,
                         "low": price - 1, "close": price})


H1_WORKED = _bars(["2025-01-07 08:00", "2025-01-07 09:00", "2025-01-07 10:00", "2025-01-07 11:00"])
H4_WORKED = _bars(["2025-01-07 04:00", "2025-01-07 08:00", "2025-01-07 12:00"])


@pytest.fixture
def app(make_settings) -> Iterator[tuple[FastAPI, TestClient]]:
    application = create_app(make_settings())
    with TestClient(application, client=("127.0.0.1", 50000)) as client:
        yield application, client


# --- build_context ---------------------------------------------------------------------------------------

def test_defaults_before_any_update(app) -> None:
    application, _ = app
    ctx = build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=application.state.db)
    assert ctx.account == DEFAULT_ACCOUNT
    assert ctx.params == SCHEMA.defaults()
    assert ctx.symbol == "XAUUSD.x" and ctx.has_open_trade is False and ctx.symbol_spec is None


def test_settings_and_params_changed_via_api_are_what_build_context_returns(app) -> None:
    application, client = app
    assert client.put("/settings", json={"balance": 2500, "risk_pct": 0.5, "leverage": 50, "rr": 3}).status_code == 200
    client.get("/strategies")  # v1 = defaults
    saved = client.put("/strategies/stddev_channel", json={"params": {"n": 80, "k": 2.5}}).json()
    ctx = build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=application.state.db, has_open_trade=True,
                        symbol_spec=GOLD)
    assert ctx.account == AccountSettings(balance=2500, risk_pct=0.5, leverage=50, rr=3)
    assert ctx.account.risk_amount == 12.5  # 2500 * 0.5 / 100
    assert ctx.params == {**SCHEMA.defaults(), "n": 80, "k": 2.5}
    assert params_hash(ctx.params) == saved["params_hash"]  # provenance matches the stored version
    assert ctx.has_open_trade is True and ctx.symbol_spec is GOLD
    cls, record = load_active_params(application.state.db)
    assert cls is StdDevChannelStrategy and record.version == saved["params_version"] == 2

    client.put("/settings", json={"risk_pct": 2})  # a later change is picked up by the next context
    assert build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=application.state.db).account.risk_pct == 2.0


def test_h4_filtered_to_bars_closed_at_decision_time(app) -> None:
    """Worked example of the module docstring."""
    db = app[0].state.db
    ctx = build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=db)
    assert ctx.decision_time_utc == _utc("2025-01-07 12:00")
    assert list(ctx.h4["time"]) == [_utc("2025-01-07 04:00"), _utc("2025-01-07 08:00")]  # 12:00 is forming
    assert len(ctx.h1) == 4
    assert_closed_bars(ctx, ctx.decision_time_utc)  # the contract evaluate_checked enforces

    live = build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=db,
                         now_utc=datetime(2025, 1, 7, 11, 30, tzinfo=timezone.utc))
    assert list(live.h1["time"]) == [_utc("2025-01-07 08:00"), _utc("2025-01-07 09:00"), _utc("2025-01-07 10:00")]
    assert live.decision_time_utc == _utc("2025-01-07 11:00")
    assert list(live.h4["time"]) == [_utc("2025-01-07 04:00")]  # 08:00 H4 closes at 12:00 > 11:00


def test_errors(app) -> None:
    db = app[0].state.db
    with pytest.raises(ValueError, match="symbol_spec is for 'BRNUSD.x'"):
        build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=db, symbol_spec=BRENT)
    with pytest.raises(NoClosedBarError):
        build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=db,
                      now_utc=datetime(2025, 1, 7, 8, 59, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        build_context("../evil", H1_WORKED, H4_WORKED, db=db)
    naive = H4_WORKED.assign(time=H4_WORKED["time"].dt.tz_localize(None))
    with pytest.raises(ValueError, match="timezone-aware"):
        build_context("XAUUSD.x", H1_WORKED, naive, db=db)


def test_end_to_end_decision_uses_stored_rr_and_never_sees_future_h4(app) -> None:
    """Full (future-including) H4 history in, evaluate_checked passes, candidate == scan's, rr from settings."""
    application, client = app
    client.put("/settings", json={"rr": 3})
    h1, h4 = random_walk(1500, seed=7)
    account = AccountSettings(rr=3)
    strategy = StdDevChannelStrategy()
    expected = {c.confirmation_bar_open_utc: c for c in strategy.scan(h1, h4, {}, account, symbol="XAUUSD.x")}
    assert expected
    checked = 0
    for when, cand in list(expected.items())[:5]:
        t = int((h1["time"] == pd.Timestamp(when)).to_numpy().nonzero()[0][0])
        ctx = build_context("XAUUSD.x", h1.iloc[: t + 1], h4, db=application.state.db)
        assert (ctx.h4["time"] + H4_DURATION <= ctx.decision_time_utc).all()
        got = evaluate_checked(strategy, ctx, params_hash(ctx.params))
        assert got == cand and got.rr == 3.0
        checked += 1
    assert checked == 5
    # The raw (unfiltered) frame would have violated the contract -> the filtering is what makes it pass.
    t = int((h1["time"] == pd.Timestamp(next(iter(expected)))).to_numpy().nonzero()[0][0])
    raw = build_context("XAUUSD.x", h1.iloc[: t + 1], h4, db=application.state.db)
    object.__setattr__(raw, "h4", h4)
    with pytest.raises(LookAheadError):
        evaluate_checked(strategy, raw)


def test_build_context_guarded_log(app, make_settings) -> None:
    db = app[0].state.db
    for dev, expect in ((True, True), (False, False)):
        stream = io.StringIO()
        logging_setup.configure_logging(make_settings(f"DEV_MODE={'true' if dev else 'false'}\n"),
                                        stream=stream, force=True)
        build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=db)
        text = stream.getvalue()
        assert ("build_context XAUUSD.x @ 2025-01-07T12:00:00+00:00: stddev_channel code v1 params v1" in text) is expect
        assert "h4=2 (not closed dropped 1)" in text if expect else text == ""


# --- size_from_spec with the real specs -----------------------------------------------------------------

def test_gold_worked_example_with_real_spec() -> None:
    r = size_from_spec((2000.00, 1995.00), WORKED_ACCOUNT, GOLD)  # SL 5.00 away
    assert r.accepted and r.warnings == [] and r.reason_fa is None
    assert r.risk_amount == 10.0            # 1000 * 1 / 100
    assert r.value_per_unit == 100.0        # tick_value 1.0 / tick_size 0.01
    assert r.loss_per_lot == 500.0          # 5.00 * 100
    assert r.volume == 0.02                 # 10 / 500 = 0.02 (floored to 0.01 steps)
    assert r.actual_risk == 10.0
    assert r.margin == 40.0                 # 0.02 * 100 * 2000 / 100


def test_brent_worked_example_with_real_spec() -> None:
    r = size_from_spec((80.00, 79.50), WORKED_ACCOUNT, BRENT)  # SL 0.50 away
    assert r.accepted and r.warnings == []
    assert r.value_per_unit == 1000.0       # tick_value 10 / tick_size 0.01
    assert r.loss_per_lot == 500.0          # 0.50 * 1000
    assert r.volume == 0.02
    assert r.actual_risk == 10.0
    assert r.margin == 16.0                 # 0.02 * 1000 * 80 / 100


def _candidate(symbol: str = "XAUUSD.x") -> SignalCandidate:
    open_t = datetime(2025, 1, 7, 10, 0, tzinfo=timezone.utc)
    return SignalCandidate(
        strategy_name="stddev_channel", strategy_version=1, params_hash="0" * 64, symbol=symbol,
        direction="buy", setup="bounce_lower", line="lower", decision_time_utc=open_t + timedelta(hours=1),
        confirmation_bar_open_utc=open_t, reference_price=2000.00, stop_loss=1995.00, rr=2.0,
        pattern="pin_bar", reason_fa="نمونه",
    )


def test_candidate_levels_indicative_and_at_fill() -> None:
    cand = _candidate()
    indicative = size_from_spec(cand, WORKED_ACCOUNT, GOLD)  # entry = reference_price 2000.00
    assert (indicative.volume, indicative.margin) == (0.02, 40.0)
    filled = size_from_spec(cand, WORKED_ACCOUNT, GOLD, entry=2001.00)  # next bar opened at 2001.00
    # SL 6.00 away -> loss/lot 600 -> raw 0.016667 -> 0.01 lots; risk 6.00; margin 0.01*100*2001/100 = 20.01
    assert filled.loss_per_lot == 600.0 and filled.volume == 0.01
    assert filled.actual_risk == 6.0 and filled.margin == pytest.approx(20.01, abs=1e-12)


def test_account_from_settings_changes_volume(app) -> None:
    application, client = app
    client.put("/settings", json={"balance": 2000})  # risk 1 % of 2000 = 20.00 -> 20 / 500 = 0.04 lots
    ctx = build_context("XAUUSD.x", H1_WORKED, H4_WORKED, db=application.state.db, symbol_spec=GOLD)
    r = size_from_spec((2000.00, 1995.00), ctx.account, ctx.symbol_spec)
    assert (r.volume, r.actual_risk, r.margin) == (0.04, 20.0, 80.0)


def test_size_from_spec_argument_errors() -> None:
    with pytest.raises(ValueError, match="symbol spec is for 'BRNUSD.x'"):
        size_from_spec(_candidate("XAUUSD.x"), DEFAULT_ACCOUNT, BRENT)
    with pytest.raises(TypeError):
        size_from_spec((2000.0, 1995.0), DEFAULT_ACCOUNT, GOLD, entry=2001.0)
    with pytest.raises(TypeError):
        size_from_spec((2000.0,), DEFAULT_ACCOUNT, GOLD)  # type: ignore[arg-type]
    # Bad symbol data -> rejected with a Persian reason, not an exception.
    closed_market = GOLD.model_copy(update={"trade_tick_value": 0.0})
    r = size_from_spec((2000.0, 1995.0), DEFAULT_ACCOUNT, closed_market)
    assert not r.accepted and "tick_value" in r.reason_fa
