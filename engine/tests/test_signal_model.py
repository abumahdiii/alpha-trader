from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from alpha_engine.strategy.signal import SETUP_LINE, Setup, SignalCandidate

HASH = "a" * 64
CONF_OPEN = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)  # confirmation H1 bar 13:00-14:00 UTC


def _signal(**overrides: object) -> SignalCandidate:
    base: dict[str, object] = dict(
        strategy_name="stddev_channel",
        strategy_version=1,
        params_hash=HASH,
        symbol="XAUUSD.x",
        direction="buy",
        setup="bounce_lower",
        line="lower",
        decision_time_utc=CONF_OPEN + timedelta(hours=1),
        confirmation_bar_open_utc=CONF_OPEN,
        reference_price=2000.00,
        stop_loss=1995.00,
        rr=2.0,
        pattern="pin_bar",
        reason_fa="برگشت از خط پایین با پین‌بار صعودی",
        extra={"mid": 2010.5, "upper": 2030.1, "lower": 1990.9, "slope": 0.12, "sigma": 9.8,
               "atr_h1": 4.1, "atr_h4": 9.0, "is_flat": False},
    )
    base.update(overrides)
    return SignalCandidate(**base)  # type: ignore[arg-type]


def test_worked_example_buy() -> None:
    # ref 2000.00, sl 1995.00, rr 2 -> indicative tp = 2000 + 2*5 = 2010.00
    sig = _signal()
    assert sig.indicative_take_profit == 2010.00
    # fill at the open of bar t+1 = 2001.00 -> risk 6.00 -> tp = 2001 + 2*6 = 2013.00
    assert sig.resolve_levels(2001.00) == (1995.00, 2013.00)
    assert sig.entry_rule == "next_bar_open"
    assert sig.fill_bar_open_utc == datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)


def test_worked_example_sell() -> None:
    # Brent sell: ref 80.00, sl 80.50, rr 2 -> tp 79.00; fill 79.90 -> risk 0.60 -> tp 78.70
    sig = _signal(symbol="BRNUSD.x", direction="sell", setup="bounce_upper", line="upper",
                  reference_price=80.00, stop_loss=80.50)
    assert sig.indicative_take_profit == pytest.approx(79.00, abs=1e-12)
    sl, tp = sig.resolve_levels(79.90)
    assert sl == 80.50 and tp == pytest.approx(78.70, abs=1e-12)


def test_no_entry_field_at_decision_time() -> None:
    fields = set(SignalCandidate.model_fields)
    assert not {"entry", "entry_price", "take_profit", "volume"} & fields
    with pytest.raises(ValidationError):
        _signal(entry_price=2001.0)


def test_frozen() -> None:
    sig = _signal()
    with pytest.raises(ValidationError):
        sig.stop_loss = 1990.0  # type: ignore[misc]


def test_json_round_trip_keeps_derived_tp() -> None:
    sig = _signal()
    again = SignalCandidate.model_validate_json(sig.model_dump_json())
    assert again == sig
    with pytest.raises(ValidationError):
        _signal(indicative_take_profit=2011.0)


@pytest.mark.parametrize(
    "overrides",
    [
        dict(stop_loss=2000.00),                                    # buy: sl must be < ref
        dict(stop_loss=2001.00),
        dict(direction="sell", setup="bounce_mid", line="mid", stop_loss=1999.0),  # sell: sl must be > ref
        dict(rr=0.0),
        dict(rr=-1.0),
        dict(reference_price=float("nan")),
        dict(decision_time_utc=datetime(2026, 9, 21, 14, 0)),       # naive
        dict(confirmation_bar_open_utc=datetime(2026, 9, 21, 13, 0)),
        dict(decision_time_utc=datetime(2026, 9, 21, 17, 0, tzinfo=timezone(timedelta(hours=3)))),  # non-UTC
        dict(decision_time_utc=CONF_OPEN + timedelta(minutes=30)),  # not the bar close
        dict(decision_time_utc=CONF_OPEN),
        dict(setup="bounce_lower", line="mid"),                     # line inconsistent with setup
        dict(direction="sell", stop_loss=2005.0),                   # bounce_lower is buy-only
        dict(direction="buy", setup="breakout_lower_pullback", line="lower"),  # sell-only setup
        dict(params_hash="xyz"),
        dict(strategy_version=0),
        dict(entry_rule="same_bar_close"),
        dict(extra={"atr_h1": float("inf")}),
        dict(reason_fa=""),
        dict(direction="long"),
        dict(setup="bounce_side"),
        dict(direction="sell", setup="bounce_upper", line="upper", stop_loss=2005.0, rr=20.0,
             reference_price=100.0),  # sell tp would be negative: 100 - 20*1905
    ],
)
def test_validators_reject(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _signal(**overrides)


def test_utc_normalised() -> None:
    import zoneinfo

    utc_zone = zoneinfo.ZoneInfo("UTC")
    sig = _signal(decision_time_utc=datetime(2026, 9, 21, 14, 0, tzinfo=utc_zone))
    assert sig.decision_time_utc.tzinfo is timezone.utc


def test_mid_setups_allow_both_directions() -> None:
    assert _signal(setup="bounce_mid", line="mid").direction == "buy"
    sell = _signal(setup="breakout_mid_pullback", line="mid", direction="sell", stop_loss=2006.0)
    assert sell.indicative_take_profit == 2000.0 - 2.0 * 6.0


def test_setup_line_table_complete() -> None:
    assert set(SETUP_LINE) == set(Setup)
    assert {s.value for s in Setup} == {
        "bounce_lower", "bounce_mid", "bounce_upper",
        "breakout_upper_pullback", "breakout_mid_pullback", "breakout_lower_pullback",
    }


@pytest.mark.parametrize("entry", [1995.00, 1990.0, 0.0, -5.0, float("nan"), float("inf"), True, "2001"])
def test_resolve_levels_rejects_bad_entry(entry: object) -> None:
    with pytest.raises(ValueError):
        _signal().resolve_levels(entry)  # type: ignore[arg-type]


def test_resolve_levels_sell_gap_through_stop() -> None:
    sig = _signal(direction="sell", setup="bounce_upper", line="upper", stop_loss=2005.0)
    with pytest.raises(ValueError):
        sig.resolve_levels(2005.0)
    with pytest.raises(ValueError):
        sig.resolve_levels(2006.0)
